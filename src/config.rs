//! Per-event configuration (spec §5.5): TOML schema, loader, local validation.
//!
//! Live-Kalshi validation (tickers exist and are open, rules text pulled) is
//! deliberately behind the `MarketValidator` trait so M2 can slot in a real
//! implementation without touching the loader.

use std::path::Path;

use serde::Deserialize;
use thiserror::Error;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventConfig {
    pub event: EventSection,
    #[serde(default)]
    pub pipeline: PipelineSection,
    pub sources: Vec<SourceConfig>,
    pub kalshi: KalshiSection,
    #[serde(default)]
    pub risk: crate::risk::RiskConfig,
    pub keywords: Vec<KeywordConfig>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventSection {
    pub id: String,
    pub starts_at: String, // RFC 3339; parsed lazily — M0 never schedules
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PipelineSection {
    pub chunk_ms: u32,
}

impl Default for PipelineSection {
    fn default() -> Self {
        PipelineSection { chunk_ms: 160 }
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum SourceConfig {
    /// M0 test harness: plays a WAV with original (or scaled) timing.
    Replay {
        path: String,
        #[serde(default = "default_speed")]
        speed: f32, // 0 = as fast as possible
    },
    /// Earnings-call dial-in (adapter lands in M2/M3; parses now).
    Sip {
        number: String,
        #[serde(default)]
        dtmf: String,
        dial_at: String,
    },
    /// Webcast fallback (adapter lands in M3; parses now).
    Hls { url: String },
}

fn default_speed() -> f32 {
    1.0
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KalshiSection {
    pub series: String,
    #[serde(default = "default_env")]
    pub env: String, // "prod" | "demo"
    #[serde(default = "default_heartbeat")]
    pub heartbeat_secs: u64,
}

fn default_env() -> String {
    "prod".into()
}
fn default_heartbeat() -> u64 {
    15
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Policy {
    FastOnly,
    ConfirmedOnly,
    TwoTier,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeywordConfig {
    pub market_ticker: String,
    pub phrase: String,
    /// Compiled from the market rules text, not from intuition (spec §5.4).
    pub variants: Vec<String>,
    pub max_price_cents: u8,
    pub budget_contracts: u32,
    pub policy: Policy,
    #[serde(default)]
    pub rule_risk_haircut_cents: u8,
}

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("failed to read {path}: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("failed to parse TOML: {0}")]
    Parse(#[from] toml::de::Error),
    #[error("config invalid: {0}")]
    Invalid(String),
}

pub fn load(path: &Path) -> Result<EventConfig, ConfigError> {
    let text = std::fs::read_to_string(path).map_err(|source| ConfigError::Io {
        path: path.display().to_string(),
        source,
    })?;
    let cfg: EventConfig = toml::from_str(&text)?;
    validate(&cfg)?;
    Ok(cfg)
}

fn validate(cfg: &EventConfig) -> Result<(), ConfigError> {
    let fail = |msg: String| Err(ConfigError::Invalid(msg));

    if cfg.sources.is_empty() {
        return fail("at least one source is required".into());
    }
    if cfg.keywords.is_empty() {
        return fail("at least one keyword is required".into());
    }
    if cfg.pipeline.chunk_ms == 0 || cfg.pipeline.chunk_ms > 1000 {
        return fail(format!(
            "pipeline.chunk_ms must be in 1..=1000, got {}",
            cfg.pipeline.chunk_ms
        ));
    }

    let mut seen = std::collections::HashSet::new();
    for kw in &cfg.keywords {
        if !seen.insert(kw.market_ticker.as_str()) {
            return fail(format!("duplicate market_ticker {}", kw.market_ticker));
        }
        if kw.variants.is_empty() {
            return fail(format!("keyword {:?} has no variants", kw.phrase));
        }
        if !(1..=99).contains(&kw.max_price_cents) {
            return fail(format!(
                "keyword {:?}: max_price_cents must be 1..=99, got {}",
                kw.phrase, kw.max_price_cents
            ));
        }
        if kw.budget_contracts == 0 {
            return fail(format!("keyword {:?}: budget_contracts must be > 0", kw.phrase));
        }
    }
    Ok(())
}

/// M2 seam: validates config against live Kalshi state at arm time (tickers
/// exist and are open, rules text stored for audit). Refuse to arm on failure.
pub trait MarketValidator {
    fn validate(&self, cfg: &EventConfig) -> Result<(), ConfigError>;
}

/// M0/M1: no live exchange access — always passes.
pub struct NoopMarketValidator;

impl MarketValidator for NoopMarketValidator {
    fn validate(&self, _cfg: &EventConfig) -> Result<(), ConfigError> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn example_path() -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("configs/example-event.toml")
    }

    #[test]
    fn parses_checked_in_example_verbatim() {
        let cfg = load(&example_path()).unwrap();
        assert_eq!(cfg.event.id, "AAPL-Q3-2026");
        assert_eq!(cfg.pipeline.chunk_ms, 160);
        assert_eq!(cfg.sources.len(), 1);
        assert!(matches!(cfg.sources[0], SourceConfig::Replay { .. }));
        assert_eq!(cfg.keywords.len(), 1);
        assert_eq!(cfg.keywords[0].max_price_cents, 97);
        assert_eq!(cfg.keywords[0].policy, Policy::TwoTier);
    }

    fn base_toml() -> String {
        std::fs::read_to_string(example_path()).unwrap()
    }

    #[test]
    fn rejects_unknown_source_kind() {
        let text = base_toml().replace("kind  = \"replay\"", "kind  = \"carrier_pigeon\"");
        assert!(toml::from_str::<EventConfig>(&text).is_err());
    }

    #[test]
    fn rejects_price_cap_100() {
        let text = base_toml().replace("max_price_cents         = 97", "max_price_cents         = 100");
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_err());
    }

    #[test]
    fn rejects_empty_variants() {
        let text = base_toml().replace(
            "variants                = [\"artificial intelligence\", \"artificial intelligences\"]",
            "variants                = []",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_err());
    }

    #[test]
    fn rejects_duplicate_ticker() {
        let mut text = base_toml();
        let dup = text[text.find("[[keywords]]").unwrap()..].to_string();
        text.push_str("\n");
        text.push_str(&dup);
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_err());
    }
}
