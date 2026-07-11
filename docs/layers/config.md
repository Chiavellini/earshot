# Layer: config (cross-cutting plane)

## Role
Per-event TOML (spec §5.5) parsed and validated at arm time into typed
structs that other layers consume by value. NEVER consulted on the hot path
— everything hot is precompiled from it (decision table, pre-staged orders,
risk gate).

## Entry door
- **Type:** a TOML file (schema: `configs/example-event.toml`, drill variant
  `configs/m2-drill.toml`).
- **Mechanism:** `config::load(path) -> Result<EventConfig, ConfigError>`
  (`src/config.rs`): parse (serde, `deny_unknown_fields` everywhere) then
  local validation (≥1 source, ≥1 keyword, chunk_ms 1..=1000, unique
  tickers, non-empty variants, cap 1..=99, budget > 0).

## Exit door
- **Type:** `EventConfig { event, pipeline, sources: Vec<SourceConfig>,
  kalshi: KalshiSection, risk: risk::RiskConfig, keywords:
  Vec<KeywordConfig> }`.
- **Consumers:** `main.rs` wires sources; `DecisionTable::from_keywords` and
  `executor::prestage_orders` compile the keyword table (index order IS
  `KeywordId`); `RiskGate::new` clamps the `[risk]` block;
  `LiveMarketValidator::validate` checks it against live Kalshi at arm time
  and stores rules text to `state/rules/` (refuses to arm on any miss).

## Obligations
- `variants` are compiled from the market rules text, not intuition
  (spec §5.4) — the loader trusts them; the process that produces them must
  cite the rules (see VERIFICATIONS Q4 for what counts as a mention).
- Keyword order is load-bearing: `KeywordId(i)` = index in `[[keywords]]`.
- Risk values only tighten: `RiskConfig::clamped()` runs in `RiskGate::new`.
- Secrets NEVER appear here — credentials come from env
  (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`), see `~/.kalshi/`.

## Failure policy
- Any parse/validation failure → refuse to start (fail cheap). Unknown TOML
  keys are errors, not warnings — typos must not silently no-op.

## How to replace or edit this layer
- Everything is `src/config.rs` + the example TOML (kept parse-tested:
  `parses_checked_in_example_verbatim`). Adding a field: serde default for
  back-compat, validation rule, update the example + this doc + the
  consuming layer's doc.

## How to test in isolation
- Unit tests in `config.rs` parse the checked-in example verbatim and assert
  rejection cases (unknown source kind, cap=100, empty variants, dup ticker).
