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
    #[serde(default)]
    pub detector: DetectorSection,
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
    #[serde(default = "default_chunk_ms")]
    pub chunk_ms: u32,
    /// Preproc upsamples any source below this rate (M1: 16 kHz only, for
    /// 16 kHz-native ASR models fed by 8 kHz telephony). Absent = passthrough.
    #[serde(default)]
    pub resample_to_hz: Option<u32>,
}

fn default_chunk_ms() -> u32 {
    160
}

impl Default for PipelineSection {
    fn default() -> Self {
        PipelineSection { chunk_ms: 160, resample_to_hz: None }
    }
}

/// `[detector]` — engine selection (spec §5.4: the M1 bake-off dial).
#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DetectorSection {
    #[serde(default)]
    pub engine: EngineKind,
    /// Required iff `engine = "sherpa_kws"`.
    pub sherpa_kws: Option<SherpaKwsConfig>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EngineKind {
    /// RMS-threshold pipeline self-test (fires on the tone fixture).
    #[default]
    Stub,
    /// sherpa-onnx streaming keyword spotter (Zipformer transducer).
    SherpaKws,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SherpaKwsConfig {
    /// Unpacked model directory (see `scripts/fetch-models.sh`): encoder /
    /// decoder / joiner .onnx + tokens.txt.
    pub model_dir: std::path::PathBuf,
    #[serde(default = "default_num_threads")]
    pub num_threads: u32,
    #[serde(default = "default_max_active_paths")]
    pub max_active_paths: u32,
    /// Per-keyword-entry firing threshold unless overridden on the keyword.
    #[serde(default = "default_kws_threshold")]
    pub default_threshold: f32,
    /// Acoustic boost applied to keyword token paths during search.
    #[serde(default = "default_kws_boost")]
    pub default_boost: f32,
    /// Duplicate suppression per (keyword, tier), in stream-time ms.
    #[serde(default = "default_refractory_ms")]
    pub refractory_ms: u64,
}

fn default_num_threads() -> u32 {
    1
}
fn default_max_active_paths() -> u32 {
    4
}
fn default_kws_threshold() -> f32 {
    0.25
}
fn default_kws_boost() -> f32 {
    2.0
}
fn default_refractory_ms() -> u64 {
    2000
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
    /// Earnings-call dial-in via Twilio Media Streams (M3): we run the WS
    /// server, Twilio's <Stream> connects to it (docs/TWILIO.md). The dial
    /// itself happens out-of-band (scripts/twilio-dial.sh).
    TwilioStream { bind: String },
    /// Raw SIP dial-in (adapter not built; parses now).
    Sip {
        number: String,
        #[serde(default)]
        dtmf: String,
        dial_at: String,
    },
    /// Webcast fallback (adapter not built; parses now).
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
    /// Partial-word prefixes that fire `Tier::Fast` before the word completes
    /// (e.g. "reces" for "recession"). Each must be a strict case-insensitive
    /// prefix of some variant. Empty ⇒ the engine emits no Fast tier.
    #[serde(default)]
    pub fast_prefixes: Vec<String>,
    /// Engine firing thresholds; None ⇒ `[detector.sherpa_kws]` defaults.
    #[serde(default)]
    pub fast_threshold: Option<f32>,
    #[serde(default)]
    pub confirmed_threshold: Option<f32>,
    #[serde(default)]
    pub boost: Option<f32>,
    /// Decision-layer confidence gates per tier; 0.0 = no gating (M0 behavior).
    #[serde(default)]
    pub min_confidence_fast: f32,
    #[serde(default)]
    pub min_confidence_confirmed: f32,
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
    for s in &cfg.sources {
        if let SourceConfig::TwilioStream { bind } = s {
            if bind.parse::<std::net::SocketAddr>().is_err() {
                return fail(format!(
                    "twilio_stream.bind {bind:?} is not a socket address (e.g. \"127.0.0.1:8080\")"
                ));
            }
        }
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
    if let Some(hz) = cfg.pipeline.resample_to_hz {
        if hz != 16_000 {
            return fail(format!(
                "pipeline.resample_to_hz: only 16000 is supported in M1 (2x from 8 kHz telephony), got {hz}"
            ));
        }
    }
    if cfg.detector.engine == EngineKind::SherpaKws && cfg.detector.sherpa_kws.is_none() {
        return fail("detector.engine = \"sherpa_kws\" requires a [detector.sherpa_kws] section".into());
    }
    if let Some(sk) = &cfg.detector.sherpa_kws {
        if !(0.0..=1.0).contains(&sk.default_threshold) || sk.default_threshold == 0.0 {
            return fail(format!(
                "detector.sherpa_kws.default_threshold must be in (0, 1], got {}",
                sk.default_threshold
            ));
        }
        if sk.num_threads == 0 {
            return fail("detector.sherpa_kws.num_threads must be > 0".into());
        }
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
        for p in &kw.fast_prefixes {
            let p_lc = p.to_lowercase();
            let is_strict_prefix = kw
                .variants
                .iter()
                .any(|v| v.to_lowercase().starts_with(&p_lc) && v.len() > p.len());
            if !is_strict_prefix {
                return fail(format!(
                    "keyword {:?}: fast_prefix {p:?} is not a strict prefix of any variant (typo?)",
                    kw.phrase
                ));
            }
        }
        for (name, t) in [
            ("fast_threshold", kw.fast_threshold),
            ("confirmed_threshold", kw.confirmed_threshold),
        ] {
            if let Some(t) = t {
                if !(t > 0.0 && t <= 1.0) {
                    return fail(format!("keyword {:?}: {name} must be in (0, 1], got {t}", kw.phrase));
                }
            }
        }
        for (name, c) in [
            ("min_confidence_fast", kw.min_confidence_fast),
            ("min_confidence_confirmed", kw.min_confidence_confirmed),
        ] {
            if !(0.0..=1.0).contains(&c) {
                return fail(format!("keyword {:?}: {name} must be in [0, 1], got {c}", kw.phrase));
            }
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

    #[test]
    fn parses_checked_in_twilio_live_config_verbatim() {
        let path =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("configs/twilio-live.toml");
        let cfg = load(&path).unwrap();
        assert!(matches!(cfg.sources[0], SourceConfig::TwilioStream { .. }));
        assert_eq!(cfg.detector.engine, EngineKind::SherpaKws);
        // Mention-probe posture invariants: every keyword 1¢ × 1 contract,
        // confirmed-only, and allowlisted.
        for kw in &cfg.keywords {
            assert_eq!(kw.max_price_cents, 1, "{}", kw.market_ticker);
            assert_eq!(kw.budget_contracts, 1, "{}", kw.market_ticker);
            assert_eq!(kw.policy, Policy::ConfirmedOnly, "{}", kw.market_ticker);
            assert!(cfg.risk.allowlist_tickers.contains(&kw.market_ticker));
        }
    }

    #[test]
    fn rejects_bad_twilio_bind() {
        let text = base_toml().replace(
            "kind  = \"replay\"\npath  = \"fixture.wav\"\nspeed = 1.0                       # 0 = as fast as possible (tests)",
            "kind = \"twilio_stream\"\nbind = \"not-an-addr\"",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(super::validate(&cfg).is_err());
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
    fn detector_defaults_to_stub() {
        let cfg = load(&example_path()).unwrap();
        assert_eq!(cfg.detector.engine, EngineKind::Stub);
        assert!(cfg.detector.sherpa_kws.is_none());
        assert_eq!(cfg.pipeline.resample_to_hz, None);
        assert!(cfg.keywords[0].fast_prefixes.is_empty());
        assert_eq!(cfg.keywords[0].min_confidence_fast, 0.0);
    }

    #[test]
    fn sherpa_engine_requires_its_section() {
        let text = base_toml() + "\n[detector]\nengine = \"sherpa_kws\"\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains("[detector.sherpa_kws]"), "{err}");
    }

    #[test]
    fn sherpa_section_parses_with_defaults() {
        let text = base_toml()
            + "\n[detector]\nengine = \"sherpa_kws\"\n[detector.sherpa_kws]\nmodel_dir = \"models/kws\"\n";
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        validate(&cfg).unwrap();
        let sk = cfg.detector.sherpa_kws.unwrap();
        assert_eq!(sk.num_threads, 1);
        assert_eq!(sk.max_active_paths, 4);
        assert_eq!(sk.default_threshold, 0.25);
        assert_eq!(sk.default_boost, 2.0);
        assert_eq!(sk.refractory_ms, 2000);
    }

    #[test]
    fn rejects_non_16k_resample_target() {
        let text = base_toml().replace("chunk_ms = 160", "chunk_ms = 160\nresample_to_hz = 44100");
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        assert!(validate(&cfg).is_err());
    }

    #[test]
    fn rejects_fast_prefix_that_matches_no_variant() {
        let text = base_toml().replace(
            "policy                  = \"two_tier\"",
            "policy                  = \"two_tier\"\nfast_prefixes           = [\"artifishul\"]",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        let err = validate(&cfg).unwrap_err().to_string();
        assert!(err.contains("fast_prefix"), "{err}");
    }

    #[test]
    fn accepts_valid_fast_prefix_case_insensitive() {
        let text = base_toml().replace(
            "policy                  = \"two_tier\"",
            "policy                  = \"two_tier\"\nfast_prefixes           = [\"Artificial intel\"]",
        );
        let cfg: EventConfig = toml::from_str(&text).unwrap();
        validate(&cfg).unwrap();
    }

    #[test]
    fn rejects_out_of_range_min_confidence() {
        let text = base_toml().replace(
            "policy                  = \"two_tier\"",
            "policy                  = \"two_tier\"\nmin_confidence_fast     = 1.5",
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
