# Reanchored takeable-size — amd-26aug04

Anchor: OUR OWN decision_ts + bridge_offset + PM order latency (grid: 100ms, 300ms, 1000ms), NOT Kalshi's reprice. bridge_error_s=None (APPROXIMATION — see extract_decisions.py).

Hit rate assumed: 75%

| word | verdict | kalshi anchor vs reach (s) | ≥10c p50 (best scenario) | note |
|---|---|---|---:|---|
| china | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
| helios | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
| tsmc | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
| oracle | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
| frontier | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
| supply chain | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
| meta | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
| azure | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
