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
  tickers, non-empty variants, cap 1..=99, budget > 0,
  `resample_to_hz` = 16000 only, `engine = "sherpa_kws"` requires its
  `[detector.sherpa_kws]` section, each `fast_prefixes` entry is a strict
  case-insensitive prefix of some variant, thresholds in (0, 1],
  `min_confidence_*` in [0, 1], `[kalshi]` `key_id`/`private_key_path`
  all-or-nothing with `expected_key_id` == `key_id` when set and
  `expected_key_id` requiring that pair, `[twilio]` `stream_url` is a
  `wss://` URL, and `[arbiter] mode = "fanout"` requires ≥ 2 sources with
  pairwise-distinct `SourceConfig::label()`s AND a fully-default `[perf]`
  section — the core pin maps are single-chain, so fanout + tuning refuses
  to arm instead of silently stacking N pinned threads per core).

## Exit door
- **Type:** `EventConfig { event, pipeline, detector: DetectorSection,
  sources: Vec<SourceConfig>, kalshi: KalshiSection,
  twilio: Option<TwilioSection>, risk: risk::RiskConfig,
  keywords: Vec<KeywordConfig> }`. `KalshiSection` carries optional
  per-account credential binding (`key_id`, `private_key_path`,
  `expected_key_id`) and `TwilioSection` the per-account Twilio binding
  (`auth_token_path`, `stream_url`, `account_sid`, `from_number` — the last two
  consumed by `scripts/launch-event.sh`, not the binary) — both for concurrent
  multi-event isolation (`docs/MULTI-EVENT.md` §8); absent → env fallback. The
  credential-source *behavior* (from_pem vs from_env) is owned by
  `docs/layers/auth.md`.
  `DetectorSection { engine: EngineKind
  (stub | sherpa_kws, default stub), sherpa_kws: Option<SherpaKwsConfig> }`;
  `PipelineSection` carries `resample_to_hz: Option<u32>` (preproc upsample
  target, absent = passthrough); `KeywordConfig` carries the M1 detector
  tuning fields (`fast_prefixes`, `fast_threshold`, `confirmed_threshold`,
  `boost`) and decision gates (`min_confidence_fast`,
  `min_confidence_confirmed`), all defaulted for back-compat.
- **Consumers:** `main.rs` wires sources;
  `detector::factory::build_detector` selects and constructs the engine
  from `[detector]` + `[[keywords]]`; `DecisionTable::from_keywords` and
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
- **A non-empty `[risk].allowlist_tickers` must COVER every keyword's
  `market_ticker`** — `config::allowlist_covers_keywords`, called from
  `main.rs::arm` right after `load`. It is deliberately NOT part of `validate`:
  bench and replay configs legitimately carry keywords with no risk section.
  An EMPTY allowlist stays legal (honestly "no orders"), and `arm` warns loudly
  about one in dry-run, because a clean dry-run on an empty allowlist proves the
  audio path and never the fire path. Guarded by
  `no_checked_in_config_has_a_partial_allowlist` (scans every `configs/*.toml`)
  and `a_partial_allowlist_is_rejected_but_an_empty_one_is_not`.
  ⚠ The shape this exists to kill: `prepare`'s placeholder allowlist entry
  survives while `[[keywords]]` are filled in real. Non-empty, so
  `--i-am-live`'s emptiness check passes, the arm comes up clean, and then
  **every** order is refused `NotAllowlisted` at fire time. All three configs
  staged for 2026-07-28/29 shipped in exactly that state.
- Secrets NEVER appear here — the config carries only *paths* to key material
  (`[kalshi].private_key_path`, `[twilio].auth_token_path`, resolved relative
  to the config) or, when those are absent, credentials come from env
  (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `TWILIO_AUTH_TOKEN`), see
  `~/.kalshi/`. `key_id` is an access-key id, not a secret.

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
- Multi-event credential binding: `kalshi_full_binding_parses_and_validates`,
  `kalshi_key_id_without_key_path_rejected`, `kalshi_expected_key_id_mismatch_rejected`,
  `kalshi_expected_key_id_without_binding_rejected`, `twilio_section_parses_and_requires_wss`,
  and `parses_checked_in_twilio_live_config_verbatim`.
