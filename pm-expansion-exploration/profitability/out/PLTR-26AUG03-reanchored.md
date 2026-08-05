# Reanchored takeable-size — pltr-26aug03

Anchor: OUR OWN decision_ts + bridge_offset + PM order latency (grid: 100ms, 300ms, 1000ms), NOT Kalshi's reprice. bridge_error_s=2.0 (APPROXIMATION — see extract_decisions.py).

Hit rate assumed: 75%

| word | verdict | kalshi anchor vs reach (s) | ≥10c p50 (best scenario) | note |
|---|---|---|---:|---|
| healthcare | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
| anthropic | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
| warp speed | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
| ai slop | RESOLVED | 100ms:-0.50, 300ms:-0.70, 1000ms:-1.40 | $0.00 |  |
| international | NO_DECISION_TS | — | — | matched cross-venue but never armed by production (or never fired a decision) for this word — no decision_ts exists to anchor on. Expected for most words; the reanchored figure only exists on the armed(production) ^ captured(exploration) ^ said intersection. |
