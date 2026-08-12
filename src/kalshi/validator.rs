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

#[derive(Debug)]
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

            // Price-level structure gate (pinned 2026-07-15): the book cache
            // prices in integer cents; `deci_cent` markets (0.1¢ steps) would
            // silently corrupt the ladders. Absent field = older wire = cent
            // levels; anything else present must be linear_cent.
            if let Some(pls) = market["price_level_structure"].as_str() {
                if pls != "linear_cent" {
                    return Err(KalshiError::Validation(format!(
                        "market {} price_level_structure is {pls:?} (not linear_cent) — \
                         the book cache prices in integer cents; refusing to arm",
                        kw.market_ticker
                    )));
                }
            }

            // Audit trail: store the rules text alongside the session state.
            let rules = serde_json::json!({
                "ticker": kw.market_ticker,
                "rules_primary": market["rules_primary"],
                "rules_secondary": market["rules_secondary"],
                "early_close_condition": market["early_close_condition"],
                "price_level_structure": market["price_level_structure"],
                "fetched": "arm-time",
            });
            let path = rules_dir.join(format!("{}.json", kw.market_ticker));
            crate::fsio::write_atomic(
                &path,
                serde_json::to_string_pretty(&rules).unwrap().as_bytes(),
            )
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

/// The mention-probe gate (verification posture): the spend envelope stays
/// the M2 drill's (cap ≤ 1¢, probe ledger binding), but Mentions-category
/// markets are permitted — the point of the verification run is to prove
/// order acceptance and reaction timing on a real mention market. Logs
/// early-close capability per market: a mention strike that cannot close
/// early will not behave like a mention market (VERIFICATIONS §Q4).
pub fn mention_probe_gate(
    markets: &[ValidatedMarket],
    max_cap_cents: u8,
) -> Result<(), KalshiError> {
    if max_cap_cents > 1 {
        return Err(KalshiError::Risk(format!(
            "mention-probe gate: cap {max_cap_cents}¢ > 1¢ — larger size is an M4 decision, not this posture"
        )));
    }
    for m in markets {
        tracing::info!(
            ticker = %m.ticker,
            category = m.category.as_deref().unwrap_or("?"),
            can_close_early = ?m.can_close_early,
            "mention-probe target"
        );
        if m.can_close_early == Some(false) {
            tracing::warn!(
                ticker = %m.ticker,
                "can_close_early=false — this market will NOT resolve early on utterance"
            );
        }
    }
    Ok(())
}

/// The mention-FILL gate (the deliberate posture past the 1¢ no-fill probe):
/// permits Mentions-category markets AND a per-order price cap up to
/// `MENTION_FILL_CAP_CENTS`, so the executor can actually cross a real ask and
/// fill. This is the only gate that lets a mention order be marketable; real
/// spend stays bounded by the config notional caps and the $1 probe ledger
/// (enforced in `RiskGate::check_order`), which are unchanged. Same early-close
/// logging as the probe gate.
pub fn mention_fill_gate(
    markets: &[ValidatedMarket],
    max_cap_cents: u8,
) -> Result<(), KalshiError> {
    if u64::from(max_cap_cents) > crate::risk::MENTION_FILL_CAP_CENTS {
        return Err(KalshiError::Risk(format!(
            "mention-fill gate: cap {max_cap_cents}¢ > {}¢ ceiling — raise MENTION_FILL_CAP_CENTS deliberately, not per-run",
            crate::risk::MENTION_FILL_CAP_CENTS
        )));
    }
    for m in markets {
        tracing::info!(
            ticker = %m.ticker,
            category = m.category.as_deref().unwrap_or("?"),
            can_close_early = ?m.can_close_early,
            cap_cents = max_cap_cents,
            "mention-FILL target (marketable — orders may cross the ask)"
        );
        if m.can_close_early == Some(false) {
            tracing::warn!(
                ticker = %m.ticker,
                "can_close_early=false — this market will NOT resolve early on utterance"
            );
        }
    }
    Ok(())
}

/// Guaranteed-no-fill guard for the mention probe drill: refuse to send a
/// buy-YES order that would cross the current best ask. `None` ask (empty
/// book or missing field) is refused — unknown is dangerous (P3).
pub fn assert_non_crossing(
    yes_ask_cents: Option<u64>,
    price_cents: u32,
) -> Result<(), KalshiError> {
    match yes_ask_cents {
        None => Err(KalshiError::Risk(
            "no best ask visible — refusing to send (cannot prove the order won't fill)".into(),
        )),
        Some(ask) if u64::from(price_cents) >= ask => Err(KalshiError::Risk(format!(
            "price {price_cents}¢ would cross best ask {ask}¢ — mention probe must be non-marketable"
        ))),
        Some(_) => Ok(()),
    }
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

    #[test]
    fn mention_probe_gate_allows_mentions_but_keeps_probe_cap() {
        let mention = ValidatedMarket {
            ticker: "KXFEDMENTION-X".into(),
            status: "active".into(),
            category: Some("Mentions".into()),
            can_close_early: Some(true),
        };
        // Mentions category is the point of this posture.
        assert!(mention_probe_gate(std::slice::from_ref(&mention), 1).is_ok());
        // The spend cap does NOT loosen with the category.
        assert!(mention_probe_gate(std::slice::from_ref(&mention), 2).is_err());
        // A market that can't close early is warned about, not refused.
        let no_early = ValidatedMarket {
            can_close_early: Some(false),
            ..mention
        };
        assert!(mention_probe_gate(&[no_early], 1).is_ok());
    }

    #[test]
    fn mention_fill_gate_permits_bounded_marketable_cap() {
        let mention = ValidatedMarket {
            ticker: "KXFEDMENTION-X".into(),
            status: "active".into(),
            category: Some("Mentions".into()),
            can_close_early: Some(true),
        };
        // A crossable cap (e.g. to fill a ~28¢ ask) is permitted...
        assert!(mention_fill_gate(std::slice::from_ref(&mention), 30).is_ok());
        // ...up to the ceiling...
        assert!(mention_fill_gate(
            std::slice::from_ref(&mention),
            crate::risk::MENTION_FILL_CAP_CENTS as u8
        )
        .is_ok());
        // ...but not beyond it (deliberate const bump required).
        assert!(mention_fill_gate(
            std::slice::from_ref(&mention),
            crate::risk::MENTION_FILL_CAP_CENTS as u8 + 1
        )
        .is_err());
    }

    #[test]
    fn non_crossing_guard() {
        // Below the ask: safe (IOC auto-cancels unfilled).
        assert!(assert_non_crossing(Some(5), 1).is_ok());
        // At or above the ask: would fill — refused.
        assert!(assert_non_crossing(Some(1), 1).is_err());
        assert!(assert_non_crossing(Some(1), 2).is_err());
        // Unknown ask: refused (P3 — never guess).
        assert!(assert_non_crossing(None, 1).is_err());
    }
}
