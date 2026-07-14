# Layer: prepare (event-prep)

## Role
The "prior step" that turns a Kalshi earnings-mention EVENT into a ready-to-run
event config, so the operator no longer hand-types markets/tickers/variants
(the manual RUNBOOK drill-4 step). Read-only against Kalshi; emits a config
TOML for review. NOT responsible for: arming, trading, dial-in sourcing (dial-in
numbers/passcodes are not in the repo — printed as a TODO in the output).

## Entry door
- **CLI:** `earshot prepare --event-ticker <EVENT> [--out <path>] [--fillable]`
  (`src/main.rs`, `Command::Prepare`). Connects via `connect_live()` (read-only
  creds) and calls `prepare::run_prepare(&rest, event_ticker, fillable)`.
- **Data:** `RestClient::event_markets(event_ticker)` (`src/kalshi/rest.rs`) —
  paginated `GET /markets?event_ticker=`, each object carrying `ticker`,
  `yes_sub_title` (the WORD), `yes_ask_dollars`, `can_close_early`,
  `rules_primary`/`rules_secondary`.

## Exit door
- **Type:** a config TOML string (written to `--out`, default
  `configs/twilio-live-<issuer>.toml`) matching `config::EventConfig`:
  `[event] [pipeline] [detector] [detector.sherpa_kws] [[sources]] [kalshi]
  [risk]` + one `[[keywords]]` per tradeable market, with `allowlist_tickers`
  = the tradeable tickers. The file is a starting point the operator REVIEWS
  (dial-in, `starts_at`, and any best-effort variants) before arming.

## Obligations
- Parse each market's rules into `{phrase, variants, said_n_times,
  can_close_early}`. Variants follow the Kalshi rule (exact word + plural /
  possessive; `A / B` subtitles yield alternates). "Said N times" strikes are
  detected (count phrasing) and SKIPPED — the one-shot decision table cannot
  trade them.
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
- A Kalshi fetch error propagates (KalshiError); nothing is emitted partially.

## How to replace or edit this layer
- All logic in `src/prepare.rs`; the REST wrapper in `src/kalshi/rest.rs`
  (`event_markets`); the CLI seam in `src/main.rs`. The tuned-threshold library
  is `prepare::tuned_library` — keep it in sync with the frozen `bakeoff.toml`
  values (the recall sweep updates both). Emitted floats use `{:?}` so they are
  valid TOML literals (`2.0`, not `2`).

## Promotion loop (shrink the unvalidated set, no recording)
Unvalidated words ship at 1¢ observe but still LOG every fire (the decision stage
logs `decision intent`; the executor logs `LOST_RACE cap=1`). After a live call,
`scripts/validate-from-log.py --log <arm.log> --config <cfg> --transcript
<public-transcript>` estimates each word's precision/recall (COUNT mode always;
TIME mode with a `.words.json` + stream-start anchor). A word that fires cleanly
(high precision, ~0 false alarms) is PROMOTED: add it to `prepare::tuned_library`
with a threshold, and the next `prepare --fillable` auto-fills it. No audio is
recorded (respects `docs/RUNBOOK.md` "do not record"). The FULL rigorous path —
`capture-bakeoff --record-dir` (separate process) → hand-labelled
`corpus/manifests/*.toml` → `bench-detector` — is the durable follow-on for the
recall ceiling, not part of a trading run.

## How to test
- `earshot prepare --event-ticker <live event> --fillable` then
  `earshot compile-keywords --config <out>` (every keyword must tokenize) and
  a dry-run `earshot arm --config <out> --allow-mention` (markets validate,
  books go live). Verified 2026-07-14 on `KXEARNINGSMENTIONMS-26JUL15` (14
  markets, all tradeable).
