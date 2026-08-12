# Layer: prepare (event-prep)

## Role
The "prior step" that turns a Kalshi earnings-mention EVENT into a ready-to-run
event config, so the operator no longer hand-types markets/tickers/variants
(the manual RUNBOOK drill-4 step). Read-only against Kalshi; emits a config
TOML for review. NOT responsible for: arming, trading, dial-in sourcing (dial-in
numbers/passcodes are not in the repo — printed as a TODO in the output).

**Dial-in numbers/passcodes remain out of the repo — that rule stands.** They rotate
per call and live in the gitignored per-event env files (`configs/*-autorun.env`).

What IS versioned, as of 2026-07-16, is the *classification*: `configs/dial-ins.toml`
records per issuer whether a dialable listen path exists at all (`tier`), how analysts
reach the queue (`qa_mechanism`), the evidence, and `number_src` — the URL to fetch the
current digits from at prep time. None of that is secret and none of it is per-call, so
it does not conflict with the rule above; it is the durable half that was previously
re-discovered from scratch every quarter. See `docs/DIAL-INS.md`.

The one invariant that matters when reading that registry: **`qa_mechanism` and
`listen_access` are independent** (the Apple rule — AAPL has no DTMF queue and a public
listen line). A transcript can never establish `listen_access`, and a `curated`/`unknown`
verdict is never a no-go.

## Entry door
- **Stable node contract:** `control.event.qualified` supplies the read-only
  event and market facts needed to generate the configuration: event identity,
  ticker, rule text, strike subtitle, early-close posture, and observed ask.
  Preparation consumes those facts; it does not consume the live
  `runtime.order.api` contract.
- **Keyword capability:** `runtime.detector.keyword_compiler` supplies
  `TokenTable::from_bpe_model` and model-aligned `encode` validation. Prepare
  may reject variants that the configured tokenizer cannot represent, but it
  does not construct or run an acoustic engine.
- **CLI:** `earshot prepare --event-ticker <EVENT> [--out <path>] [--fillable]`
  (`src/main.rs`, `Command::Prepare`). It loads the detector vocab
  (`kws_compile::TokenTable::from_bpe_model` from `prepare::KWS_MODEL_DIR`;
  missing model ⇒ `None` + loud warning) and emits the reviewed config.
- **Migration debt — current implementation only:** the compatibility CLI
  still calls `connect_live()` and passes a `RestClient` directly to
  `prepare::run_prepare`; `RestClient::event_markets(event_ticker)` performs
  paginated `GET /markets?event_ticker=` and supplies `ticker`,
  `yes_sub_title`, `yes_ask_dollars`, `can_close_early`,
  `rules_primary`/`rules_secondary`. This is a legacy internal reach that must
  move into qualification. It is not a stable dependency on the order-transport
  node and must not be used to justify a `runtime.order.api → control.prepare`
  architecture edge.

## Exit door
- **Type:** a config TOML string (written to `--out`, default
  `configs/twilio-live-<issuer>.toml`) matching `config::EventConfig`:
  `[event] [pipeline] [detector] [detector.sherpa_kws] [[sources]] [kalshi]
  [risk]` + one `[[keywords]]` per tradeable market, with `allowlist_tickers`
  = the tradeable tickers. The file is a starting point the operator REVIEWS
  (dial-in, `starts_at`, and any best-effort variants) before arming.
- **Phone-lane derivation:** `scripts/phone-lane-derive.py` turns a staged
  webcast arm into a phone-only one — exactly one `twilio_stream` source with
  the Q-REDIAL parameters (`idle_timeout_secs = 8`, event-wide
  `max_reconnects = 20`), no `[arbiter]` (fanout requires >= 2 sources), a
  bind port `deploy/caddy/Caddyfile` actually routes, and the matching
  `configs/<ev>-phone-autorun.env`. It rewrites only `max_price_cents`,
  `budget_contracts`, and `min_confidence_confirmed`, leaving ticker/phrase/
  variants byte-identical, so `keywords_sha256` is preserved and the twin's
  readiness study and dead-word ack carry over instead of forcing a
  laptop-only TTS restudy on event morning. Caps come from `src/risk.rs`
  (`MENTION_FILL_CAP_CENTS`) and the config's
  `[risk].max_order_notional_cents`, never from memory: exceeding the first
  makes `mention_fill_gate` refuse the run, and exceeding the second returns
  `Refusal::OrderNotional` on every order.
  The reconnect budget is deliberately emitted by the generic derivation,
  never patched into an event config at fire time. It replaces only Twilio
  call legs behind one listener; `control.supervise` owns the invariant that
  the Rust arm, decision state, report and evidence writers are not restarted.
  Its autorun env also carries a literal portable
  `EARSHOT_ACCOUNT_PROFILE="$HOME/.earshot/<account-basename>"`. The runtime
  `ACCOUNT_ENV` may still use a host-search loop, but `control.operate` never
  sources generated event envs merely to discover identity; this explicit
  locator is the immutable-plan input. It contains no credential material.
- **Authoritative time input:** `scripts/slate-time-merge.py` may add a same-day
  verified time using the established `call_time_utc`/`source` fields. A time
  on a different date does **not** silently supersede discovery. The keyed
  entry must also carry an exact schema-1 `date_correction` object with kind
  `call-date-correction`, distinct `from`/`to` dates, a non-estimate source,
  and `recorded_utc`. The `from` date must match the slate row and the UTC time
  must land on `to`. The merged row preserves that typed correction and marks
  `call_date_source = "authoritative_correction"`; malformed, stale, or
  unsourced corrections are refusals and leave the row unchanged.
- **Idempotent staging repair:** a lane config containing exactly one explicit
  `starts_at ... # TODO` is incomplete, never `ALREADY-STAGED`. When a verified
  merged time later arrives, `scripts/stage-reconcile.py` may replace only that
  TODO line and `slate-stage.sh` creates the missing generic autorun env
  atomically. A complete matching config/env is read-only and reports
  `ALREADY-STAGED`; a conflicting non-TODO time or existing incomplete env
  fails loudly instead of being guessed or overwritten.
- **RunPlan freeze boundary:** before repairing or creating a staged config/env,
  `stage-reconcile.py` scans published `var/runs/*/*/control/run-plan.json`
  records. A referenced config and its event-env association are immutable.
  Their current bytes must still match the pinned hashes, and an incomplete
  pinned input is `IMMUTABLE-INCOMPLETE`; staging never rewrites the config,
  env association, or RunPlan. Recovery is a deliberate new attempt with a
  new config path, not an in-place premarket patch.

## Obligations
- Parse each market's rules into `{phrase, variants, said_n_times,
  can_close_early}`. Variants follow the Kalshi rule (exact word + plural /
  possessive; `A / B` subtitles yield alternates). "Said N times" strikes are
  detected (count phrasing) and SKIPPED — the one-shot decision table cannot
  trade them.
- **Letter-spelling for acronym-shaped alternates** (2026-07-29, after WING's
  AI market lost its live race): when a subtitle alternate is a standalone
  2–4 letter ALL-CAPS token in the ORIGINAL market phrase ("AI", "EV", "QE"),
  `prep_market` emits an ADDITIONAL letter-spelled variant ("AI" → `"a i"`).
  Deliberately conservative — mixed-case words ("Chipotle") and anything
  longer never spell; the caps form is the signal that speakers say letters.
  The joined 2-char form still dies at the `MIN_VARIANT_CHARS = 3` floor
  (that guard is MEASURED: 38 FA/event on 2-char variants — see
  `strip_short_variants`); the letter-spelled form is 3+ chars with the
  spaces and survives it, which is the whole point.
  **COUNTERWEIGHT — generation is not validation:** an auto-generated
  letter-spelling helps recall only if the readiness study
  (`scripts/variant-study.sh`) confirms the model can hear it, and the WING
  post-mortem warns an untested variant can false-fire and consume the
  one-shot. That is exactly why the blocking keyword-readiness gate in
  `scripts/preflight-event.sh` refuses to arm unstudied or zero-confirmed
  keywords — this generator only feeds the study, never bypasses it.
- **Never emit a variant the detector cannot match** (follow-up 6, closed
  2026-07-16). Raw subtitles fail in TWO ways: LOUD — a symbol not in the
  vocab ("Warner Bros." → "BROS.") makes `arm` refuse the ENTIRE config; and
  SILENT — the vocab contains `-`/`'` as pieces, so "Non-Stop" encodes to
  `▁NO N - S T O P`, tokens the transducer never emits for speech: a dead
  entry with zero recall that encode-validation alone would pass. Two stages,
  both load-bearing: `sanitize_variant` normalizes into the detector alphabet
  (lowercase letters + spaces; `-`/`_`→space, `&`→" and ", all other
  punctuation/digits stripped), then every variant is encode-validated against
  the real vocab (`TokenTable::encode`, the exact path arm uses). Unencodable
  variants are DROPPED with a `# DROPPED unencodable variant` comment; a
  keyword losing all variants is SKIPPED and excluded from
  `allowlist_tickers`; the footer counts both. With no vocab table (model
  missing) the config is emitted with a loud `# WARNING: encodability NOT
  validated` header. `preflight-event.sh` re-runs `compile-keywords` against
  the pinned binary as the belt-and-braces gate for the loud class.
- `max_price_cents` per keyword is TIERED: `--fillable` fills at `min(live ask,
  MENTION_FILL_CAP_CENTS)` ONLY for precision-vetted words (`tuned_library`);
  every unvalidated word is left at 1¢ observe (it hits the executor's LOST_RACE
  non-crossing branch — never sends, never debits the $1 ledger). This keeps a
  false alarm on an unproven word from spending money or starving the vetted
  fills. `--fillable --fill-all` overrides the tier (every word ≤ ceiling fills —
  false-alarm + ledger-starvation risk; deliberate only). Default (no flag) ⇒ 1¢
  probe for all. Asks above the ceiling or absent are always left at 1¢.
- HONESTY: tag each keyword `corpus-tuned` (thresholds from the tuned library:
  tariff/recession/guidance/acquisition) vs `corpus-UNVALIDATED` (engine
  defaults — best-effort recall). Flag compound/symbol phrases and
  `can_close_early=false` markets for operator review. Never silently imply a
  best-effort word is validated.

## Failure policy
- Empty market list (bad ticker / closed event) → error, no file written.
- While the legacy direct lookup remains, a Kalshi fetch error propagates
  (`KalshiError`); nothing is emitted partially. At the stable boundary, an
  incomplete `control.event.qualified` input is refused before emission.
- A cross-date call time without the exact typed/source-bearing correction is
  refused. An already-staged TODO without a verified time remains
  `NEEDS-TIME`; it is not counted as ready. Missing generic env creation is
  atomic and failure is nonzero. Existing RunPlan pins and non-TODO time/env
  conflicts are never repaired in place.

## How to replace or edit this layer
- All logic in `src/prepare.rs`; the REST wrapper in `src/kalshi/rest.rs`
  (`event_markets`) and the CLI seam in `src/main.rs` are the legacy reach
  described above, not part of this node's target contract. Migrate that fetch
  into `control.qualify` and pass the resulting read-only facts into prepare.
  The tuned-threshold library is `prepare::tuned_library` — keep it in sync
  with the frozen `bakeoff.toml` values (the recall sweep updates both).
  Emitted floats use `{:?}` so they are valid TOML literals (`2.0`, not `2`).

## Promotion loop (shrink the unvalidated set, no recording)
Unvalidated words ship at 1¢ observe but still LOG every fire (the decision stage
logs `decision intent`; the executor logs `LOST_RACE cap=1`). After a live call,
`scripts/validate-from-log.py --log <arm.log> --config <cfg> --transcript
<public-transcript>` estimates each word's precision/recall (COUNT mode always;
TIME mode with a `.words.json` + stream-start anchor). A word that fires cleanly
(high precision, ~0 false alarms) is PROMOTED: add it to `prepare::tuned_library`
with a threshold, and the next `prepare --fillable` auto-fills it. No audio is
recorded (respects `docs/history/RUNBOOK.md` "do not record"). The FULL rigorous path —
`capture-bakeoff --record-dir` (separate process) → hand-labelled
`corpus/manifests/*.toml` → `bench-detector` — is the durable follow-on for the
recall ceiling, not part of a trading run.

## How to test
- Unit: `cargo test prepare::` (sanitizer vectors + `prep_market` shapes);
  model-gated: `EARSHOT_REQUIRE_MODELS=1 cargo test prepare::` additionally
  proves sanitized forms encode on the real vocab and raw punctuation forms
  do not.
- Offline control-plane fixtures: `python3 scripts/slate-time-merge.py
  --self-test` and `python3 tests/stage_reconcile.py`. The latter uses generic
  ALFA/BETA events to prove typed date correction, TODO-not-ready behavior,
  missing-env creation/failure, idempotence, and byte-stable RunPlan/config
  refusal without touching issuer files or external services.
- End-to-end: `earshot prepare --event-ticker <live event> --fillable` then
  `earshot compile-keywords --config <out>` (every keyword must tokenize) and
  a dry-run `earshot arm --config <out> --allow-mention` (markets validate,
  books go live). Verified 2026-07-14 on `KXEARNINGSMENTIONMS-26JUL15` (14
  markets, all tradeable).
