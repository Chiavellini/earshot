# Node: `runtime.verifier`

## Job

Re-decode the firing leg's last `tail_ms` of audio with an OFFLINE recognizer
(unbiased second opinion) and publish a per-(keyword, tier) verdict that the
executor may hold a FILL-priced intent on; record every verdict as run evidence
so precision-per-verdict is scorable before anything is gated.

## Owns

`src/verifier/mod.rs` (`VerifierClient`, `VerifyRequest`, `Verdict`,
`VerdictRecord`, `Await`, `build_verifier` / `build_with_transcriber`,
`VerifierHandle`, `VerifierReport`), `src/verifier/tail.rs` (`PcmTail`),
`src/verifier/transcriber.rs` (`TailTranscriber`, `SherpaOfflineTranscriber`,
`MockTranscriber`), `src/verifier/report.rs` (`earshot verify-report`, the
retrospective over a banked run), `scripts/verify-join.py` (verdict × TP/FP join).

## Does not own

The `[verifier]` config schema and its validation (`runtime.config`), the
executor hold semantics and `OrderRecord.verify_*` fields
(`runtime.kalshi.executor`), the pipeline `StageHooks` and the detect-thread
request point (`runtime.pipeline` composer), arm wiring and the report JSON
(`runtime.assembly`), the shared word matcher (`runtime.detector.online_asr`),
decision one-shot semantics (`runtime.decision` — untouched: a vetoed fill
leaves the keyword consumed in v1), and money policy.

## Input contracts

`VerifierSection` (`mode = off|shadow|gate`, `model_dir`, `model_type =
nemo_transducer|nemo_ctc|transducer`, `num_threads`, `tail_ms` 1000..=10000,
`budget_ms` 100..=5000, `on_timeout = send|hold`, `sample = intents|all_confirmed`,
`tickers` — `gate` with no tickers is refused). `KeywordConfig` list for the
matcher and the opt-in map. From the detect thread: `VerifyRequest { keyword,
tier, intent_bearing, source, leg, audio_pos_ms, audio_consumed_ms, pcm
(Arc<[i16]>, the leg's last tail_ms ENDING at the firing chunk),
sample_rate_hz, decision_ts }` — posted AFTER the fire-first `try_send(intent)`,
never on the critical span. From the executor: `await_verdict(keyword, tier,
deadline)`.

## Output contracts

`runtime.verifier.verdicts`: `Verdict::{Confirmed{variant}, Rejected,
Error{message}}` per (keyword, tier) slot; `Await::{Ready(v), Timeout, Dropped,
NotRequested}`; one `VerdictRecord` row per request (ticket, phrase, leg,
intent_bearing, verdict, transcript, tail_ms, queue_wait_us, decode_us,
since_decision_us, timestamps) in `VerifierReport` → `json["verifier"]` of the
run report. **An EMPTY transcript is `Error`, never `Rejected`** — Parakeet-TDT
returned nothing for 2 of 80 real 3.5 s windows (both true fires) on HD-box
26AUG18; an instrument miss must not veto. Verdicts are only ever consulted for
`Tier::Confirmed` intents priced above `OBSERVE_PRICE_CENTS`; the 1¢ observe
path never waits (executor test `observe_never_waits_in_gate_mode`).

## Side effects and exclusive resources

One `verifier` OS thread per process owning one `OfflineRecognizer` (built at
arm; a missing asset refuses the arm); a bounded `sync_channel(8)` request
queue (`try_send` — a full queue DROPS the request, marks the slot Dropped, and
the executor never waits on it); a per-leg `PcmTail` ring inside the detect
thread (3.5 s @16 kHz = 112 KB). No network I/O. `shutdown()` then `join()`
at teardown, after the pipeline and executor have ended.

## Failure contract

Build errors (`model_dir`, `tokens.txt`, `OfflineRecognizer::create`) refuse
the arm with the path named. At run time nothing here can stop a trade except
a `Rejected` verdict in `gate` mode on an opted-in fill word, or a `Timeout`
under `on_timeout = hold`; `Error`, `Dropped`, `NotRequested` and every
`shadow`-mode outcome let the intent proceed. Every request produces a row.

## Parallel work boundary

This node may change the four source files, its script, and this doc.
Changing what the executor does with a verdict, the hold budget semantics, the
config schema, or the pipeline request point requires the owning node's claim.
`VariantMatcher::contains` semantics are shared with `runtime.detector.online_asr`.

## Verification

`cargo test --lib verifier` (slots ready-before/after wait, timeout, bounded
queue drops, sampling/shadow semantics, tail ring), `cargo test --test exec_mock`
(gate veto sends nothing; gate confirmed proceeds; timeout send/hold; shadow
never waits; observe never waits), `cargo test --lib pipeline::tests`
(fanout: one intent-bearing request per keyword, all_confirmed rows, tail ≤
tail_ms). Retrospective evidence: `earshot verify-report --run-dir <run>
--model-dir <parakeet> [--config <copy>]` + `scripts/verify-join.py`.

## Migration status

Built 2026-08-18 (detection-accuracy program D2). V0 retrospective on the
26AUG18 AS + HD-box runs with `sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8`
(box, 4 threads): decode p50 298–365 ms / p95 361–397 ms for a 3.5 s tail;
**8/8 false fires rejected** (`A terrorist`→tariff — the AS 96¢ fill;
`innovative`, `inflationary`, `storage`/`in-store`), 0 missed vetoes; false
vetoes only on proper nouns the offline model misheard (`Winburton`,
`Foot Walker`) plus 2 empty decodes (now `Error`) — the reason gating is
per-ticker opt-in and generic words go first. Not yet armed; next: shadow on
≥3 events (`mode = shadow`, `sample = all_confirmed`), then gate on opted-in
generic fill words. Config → `docs/BAKEOFF.md` §D2 bars.
