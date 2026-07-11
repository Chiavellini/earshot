//! Arm-time validation against live Kalshi state (spec §5.5 loader duties):
//! every keyword's market must exist and be open; rules text is pulled and
//! stored for audit; the M2 live gate additionally refuses Mentions-category
//! series and caps above 1¢. Refuse to arm on any failure (P3).
//!
//! Async (runs inside the arm command's runtime); the sync
//! `config::MarketValidator` trait remains for offline/noop use.

use std::path::Path;

use serde_json::Value;

use super::rest::RestClient;
use super::KalshiError;
use crate::config::EventConfig;

pub struct ValidatedMarket {
    pub ticker: String,
    pub status: String,
    pub category: Option<String>,
    pub can_close_early: Option<bool>,
}

pub struct LiveMarketValidator<'a> {
    pub rest: &'a RestClient,
    pub state_dir: &'a Path,
}

impl LiveMarketValidator<'_> {
    pub async fn validate(&self, cfg: &EventConfig) -> Result<Vec<ValidatedMarket>, KalshiError> {
        let rules_dir = self.state_dir.join("rules");
        std::fs::create_dir_all(&rules_dir)
            .map_err(|e| KalshiError::Validation(format!("state dir: {e}")))?;

        let mut out = Vec::new();
        for kw in &cfg.keywords {
            let resp = self.rest.market(&kw.market_ticker).await.map_err(|e| {
                KalshiError::Validation(format!("market {} not fetchable: {e}", kw.market_ticker))
            })?;
            let market = &resp["market"];
            if market.is_null() {
                return Err(KalshiError::Validation(format!(
                    "market {} missing in response",
                    kw.market_ticker
                )));
            }
            let status = market["status"].as_str().unwrap_or("?").to_string();
            if status != "active" {
                return Err(KalshiError::Validation(format!(
                    "market {} status is {status:?}, not active — refusing to arm",
                    kw.market_ticker
                )));
            }

            // Audit trail: store the rules text alongside the session state.
            let rules = serde_json::json!({
                "ticker": kw.market_ticker,
                "rules_primary": market["rules_primary"],
                "rules_secondary": market["rules_secondary"],
                "early_close_condition": market["early_close_condition"],
                "fetched": "arm-time",
            });
            let path = rules_dir.join(format!("{}.json", kw.market_ticker));
            std::fs::write(&path, serde_json::to_string_pretty(&rules).unwrap())
                .map_err(|e| KalshiError::Validation(format!("store rules: {e}")))?;

            let category = self.category_of(market).await;
            out.push(ValidatedMarket {
                ticker: kw.market_ticker.clone(),
                status,
                category,
                can_close_early: market["can_close_early"].as_bool(),
            });
        }
        Ok(out)
    }

    async fn category_of(&self, market: &Value) -> Option<String> {
        // Series ticker is the leading segment of the event/market ticker.
        let series_ticker = market["event_ticker"]
            .as_str()
            .or_else(|| market["ticker"].as_str())?
            .split('-')
            .next()?;
        let series = self.rest.series(series_ticker).await.ok()?;
        series["series"]["category"].as_str().map(String::from)
    }
}

/// The M2 live gate: refuses anything that could spend real money outside
/// the bounded-loss drill envelope. Marketable mention trading is an M4
/// decision, not an M2 capability.
pub fn m2_live_gate(markets: &[ValidatedMarket], max_cap_cents: u8) -> Result<(), KalshiError> {
    if max_cap_cents > 1 {
        return Err(KalshiError::Risk(format!(
            "M2 live gate: cap {max_cap_cents}¢ > 1¢ — marketable prices are not enabled this milestone"
        )));
    }
    for m in markets {
        if m.category.as_deref() == Some("Mentions") {
            return Err(KalshiError::Risk(format!(
                "M2 live gate: {} is a Mentions-category market — no mention orders this milestone",
                m.ticker
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn live_gate_refuses_marketable_caps_and_mentions() {
        let non_mention = ValidatedMarket {
            ticker: "KXHIGHNY-X".into(),
            status: "active".into(),
            category: Some("Climate and Weather".into()),
            can_close_early: Some(false),
        };
        let mention = ValidatedMarket {
            ticker: "KXFEDMENTION-X".into(),
            status: "active".into(),
            category: Some("Mentions".into()),
            can_close_early: Some(true),
        };
        assert!(m2_live_gate(std::slice::from_ref(&non_mention), 1).is_ok());
        assert!(m2_live_gate(std::slice::from_ref(&non_mention), 2).is_err());
        assert!(m2_live_gate(&[non_mention, mention], 1).is_err());
    }
}
