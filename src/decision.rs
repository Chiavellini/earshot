//! Decision engine (spec §5.6, M0 subset): precompiled keyword table with a
//! one-shot per-keyword state machine. O(1) lookup, no allocation on the
//! detection path except the intent itself.
//!
//! M2 adds here: local WS book read, sweep computation, fee/EV gate,
//! LOST_RACE branch. The M2 executor consumes `DecisionIntent`s from a ring.

use crate::clock::MonotonicNs;
use crate::config::{KeywordConfig, Policy};
use crate::types::{DecisionIntent, Detection, Tier};

/// Fraction of a keyword's budget released on a Fast-tier hit (spec §5.4:
/// small tranche sized to be an acceptable write-off). Public because the
/// executor pre-stages order bodies for exactly these tranche sizes.
pub const FAST_TRANCHE_PCT: u32 = 30;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum KeywordState {
    Armed,
    FastFired,
    Done,
}

struct Entry {
    market_ticker: String,
    max_price_cents: u8,
    budget_contracts: u32,
    policy: Policy,
    state: KeywordState,
    /// Per-tier confidence gates (config `min_confidence_fast`/`_confirmed`).
    /// 0.0 = no gating; engines without real scores emit confidence 1.0.
    min_conf_fast: f32,
    min_conf_confirmed: f32,
}

pub struct DecisionTable {
    entries: Vec<Entry>,
}

impl DecisionTable {
    pub fn from_keywords(keywords: &[KeywordConfig]) -> Self {
        let entries = keywords
            .iter()
            .map(|kw| Entry {
                market_ticker: kw.market_ticker.clone(),
                max_price_cents: kw.max_price_cents,
                budget_contracts: kw.budget_contracts,
                policy: kw.policy,
                state: KeywordState::Armed,
                min_conf_fast: kw.min_confidence_fast,
                min_conf_confirmed: kw.min_confidence_confirmed,
            })
            .collect();
        DecisionTable { entries }
    }

    /// One-shot state machine per keyword: `Armed → FastFired → Done`
    /// (or `Armed → Done` for single-tier policies). Detections after `Done`
    /// are ignored; callers log them.
    pub fn decide(&mut self, det: &Detection) -> Option<DecisionIntent> {
        let entry = self.entries.get_mut(det.keyword.0 as usize)?;

        // Confidence gate before any state transition: a gated detection must
        // not consume the one-shot machine.
        let min_conf = match det.tier {
            Tier::Fast => entry.min_conf_fast,
            Tier::Confirmed => entry.min_conf_confirmed,
        };
        if det.confidence < min_conf {
            return None;
        }

        let tranche = match (entry.state, entry.policy, det.tier) {
            (KeywordState::Done, _, _) => return None,
            (KeywordState::FastFired, _, Tier::Fast) => return None,

            (KeywordState::Armed, Policy::FastOnly, Tier::Fast) => {
                entry.state = KeywordState::Done;
                entry.budget_contracts
            }
            (KeywordState::Armed, Policy::ConfirmedOnly, Tier::Confirmed) => {
                entry.state = KeywordState::Done;
                entry.budget_contracts
            }
            (KeywordState::Armed, Policy::TwoTier, Tier::Fast) => {
                entry.state = KeywordState::FastFired;
                entry.budget_contracts * FAST_TRANCHE_PCT / 100
            }
            (KeywordState::Armed, Policy::TwoTier, Tier::Confirmed)
            | (KeywordState::FastFired, Policy::TwoTier, Tier::Confirmed) => {
                let fast = if entry.state == KeywordState::FastFired {
                    entry.budget_contracts * FAST_TRANCHE_PCT / 100
                } else {
                    0
                };
                entry.state = KeywordState::Done;
                entry.budget_contracts - fast
            }
            _ => return None, // tier/policy mismatch (e.g. Confirmed hit on fast_only)
        };

        Some(DecisionIntent {
            keyword: det.keyword,
            market_ticker: entry.market_ticker.clone(),
            max_price_cents: entry.max_price_cents,
            tranche_contracts: tranche,
            tier: det.tier,
            audio_ts: det.audio_ts,
            decision_ts: MonotonicNs::now(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::KeywordId;

    fn kw(policy: Policy) -> KeywordConfig {
        KeywordConfig {
            market_ticker: "KX-TEST".into(),
            phrase: "test".into(),
            variants: vec!["test".into()],
            max_price_cents: 97,
            budget_contracts: 200,
            policy,
            rule_risk_haircut_cents: 0,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        }
    }

    fn det(tier: Tier) -> Detection {
        Detection {
            keyword: KeywordId(0),
            confidence: 1.0,
            tier,
            audio_ts: MonotonicNs::now(),
            emit_ts: MonotonicNs::now(),
            audio_pos_ms: 0,
        }
    }

    #[test]
    fn two_tier_releases_tranche_then_remainder_then_nothing() {
        let mut table = DecisionTable::from_keywords(&[kw(Policy::TwoTier)]);
        let fast = table.decide(&det(Tier::Fast)).unwrap();
        assert_eq!(fast.tranche_contracts, 60); // 30% of 200
        let confirm = table.decide(&det(Tier::Confirmed)).unwrap();
        assert_eq!(confirm.tranche_contracts, 140);
        assert!(table.decide(&det(Tier::Fast)).is_none());
        assert!(table.decide(&det(Tier::Confirmed)).is_none());
    }

    #[test]
    fn confirmed_only_ignores_fast_hits() {
        let mut table = DecisionTable::from_keywords(&[kw(Policy::ConfirmedOnly)]);
        assert!(table.decide(&det(Tier::Fast)).is_none());
        let confirm = table.decide(&det(Tier::Confirmed)).unwrap();
        assert_eq!(confirm.tranche_contracts, 200);
        assert!(table.decide(&det(Tier::Confirmed)).is_none());
    }

    #[test]
    fn confirm_without_prior_fast_gets_full_budget() {
        let mut table = DecisionTable::from_keywords(&[kw(Policy::TwoTier)]);
        let confirm = table.decide(&det(Tier::Confirmed)).unwrap();
        assert_eq!(confirm.tranche_contracts, 200);
        assert!(table.decide(&det(Tier::Fast)).is_none());
    }

    fn det_conf(tier: Tier, confidence: f32) -> Detection {
        Detection { confidence, ..det(tier) }
    }

    #[test]
    fn confidence_gate_blocks_without_consuming_state() {
        let mut keyword = kw(Policy::TwoTier);
        keyword.min_confidence_fast = 0.6;
        keyword.min_confidence_confirmed = 0.3;
        let mut table = DecisionTable::from_keywords(&[keyword]);

        // Below-gate Fast is dropped AND the machine stays Armed…
        assert!(table.decide(&det_conf(Tier::Fast, 0.5)).is_none());
        // …so an above-gate Fast still releases the tranche afterwards.
        let fast = table.decide(&det_conf(Tier::Fast, 0.7)).unwrap();
        assert_eq!(fast.tranche_contracts, 60);
        // Per-tier independence: 0.4 fails the Fast gate but passes Confirmed.
        let confirm = table.decide(&det_conf(Tier::Confirmed, 0.4)).unwrap();
        assert_eq!(confirm.tranche_contracts, 140);
    }

    #[test]
    fn default_zero_gates_pass_everything() {
        let mut table = DecisionTable::from_keywords(&[kw(Policy::TwoTier)]);
        assert!(table.decide(&det_conf(Tier::Fast, 0.0)).is_some());
    }
}
