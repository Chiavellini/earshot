# Handoff — TLN 26AUG05 phone-race latency decomposition, and the P1 verdict

**Session:** 2026-08-05 evening → 2026-08-06. Node: `evidence.measure` (this file),
`evidence.benchmark` + `runtime.detector.streaming_ctc` (the code/doc work).
**Branch:** `webcast-lane`. **Nothing committed by this session** — see §7.

Started as a read-only investigation ("is the ~0.5–0.7 s phone-race deficit reducible?").
It ended having answered P1, a follow-on that had been open since 2026-07-21.

---

## 1. Initial project state

The brief described a lost race on 2026-08-05: market
`KXEARNINGSMENTIONTLN-26AUG05-BEHI`, phone lane, Kalshi repriced 53¢→99¢ in 404 ms and our
`decision_ts` landed **534 ms** after the sweep began. Arm ran on the CDMX laptop over ngrok
while the us-east-1 box sat idle.

Problems found at the start, all of which changed the answer:

| Premise in the brief | What the sources actually said |
|---|---|
| "~9 contracts traded in the ±10 s window" | **929.30** contracts; **612.99 at ≤97¢**, our cap |
| "`edges{}` populated, capture→decision p50 69 ms" | `edges{}` **empty** in all three morning reports. 69 ms is `detect_latency_us`, which **excludes** the 320 ms model lag |
| "`confirmed_only` waits for post-keyword audio" | `ctc_scorer.rs:21` fires "the frame the last token peaks, **not at endpointing**" — the wait is the *encoder's* quantum, not the policy |
| "host clock ~0.101 s slow" | Re-measured **+0.0097 s** (5 samples). Deficit is 534 ms, not 640 ms |
| "4.0% packet loss to the Twilio media edge" | **Zero** `twilio sequence gap` events across ~186,000 frames. The 4% is failed TCP connects to `media.twiliocdn.com`, a CDN asset host — wrong host, wrong direction (Twilio dials *us*, over TCP/WSS) |

Further inconsistencies discovered later:

- **`lost_races` is broken on fanout runs.** Reports `0` on all three 2-leg morning runs while
  carrying 11 / 12 / 5 order records with `lost_race: true`. Correct on the 1-leg TLN run.
- **P1 was mislabelled in four docs** as "chunk-16 re-export (160 ms quantum)". Chunk-16 *is*
  the armed export and is 320 ms.
- **The chunk-8 export already existed**, staged on the box since 2026-07-23, never benched.
- **A second agent session was editing the same working tree concurrently** — see §7.

---

## 2. Work completed

### 2a. The latency decomposition (read-only)

Rebuilt the budget from primary sources: the Kalshi public trade tape, the run's own
`launch.log`, and the reports' per-detection records (not `edges{}`).

**Headline: 371 of the 534 ms was inside our own process**, measured on the actual race from
the TLN `report.json` written at shutdown:

| Term | Value |
|---|---|
| `model_lag` (`audio_consumed_ms − audio_pos_ms`) | **360 ms** |
| inference (`audio_ts → emit_ts`) | 90.5 ms |
| `detect → decision` | 14 µs |
| `decision → exec` | 3,098 µs |
| **in-process total** | **371 ms** |
| residual (transport differential + competitor's own latency) | **163 ms** |

Root cause: `decode_chunk_len=32` → a **320 ms decode quantum** (`VERIFICATIONS.md`), so the
encoder emits posteriors for a word 200–480 ms after that word's audio was consumed
(MEASURED, n=251 across the three morning webcast reports).

Everything upstream of our WS socket (room, mixer, PSTN, Twilio edge) is **NOT MEASURED and
structurally unmeasurable** — but it is **common-mode** with any competitor on the same
bridge, so it cancels. The unmeasurable term is the term that does not matter.

Also found: 9,395 `ring full` dropped frames on the TLN run (≈188 s of audio) with
`chunk→detect` **max 48.7 s** — local CPU starvation on the laptop, not network loss. Only
4 detections in a 79-minute call. **This session contributed to that** (see §7).

### 2b. `bench-detector --model-dir` (new capability)

The bench gridded engine × chunk_ms only, so comparing two model exports meant two separate
invocations — indefensible on a box with documented 3.5× wall-clock run variance. Added
`model_dir` as a third grid axis so two exports run **interleaved in one invocation**.

Design decision worth keeping: a `--model-dir` that cannot be applied is a **hard refusal**,
never a silent no-op. A no-op would emit two cells that unknowingly ran the same artifact and
read as "the model made no difference" — indistinguishable from the genuine E0c null result.

### 2c. P1 answered — NEGATIVE

The export had existed on the box since 2026-07-23 (`streaming-ctc-en-20260723-chunk8`), with
a matched chunk-16 control from the same toolchain and checkpoint. Verified geometry:
**T=29, decode_chunk_len=16 → 160 ms**. P1 was never blocked on producing a model.

Ran both interleaved, lossless, dev split, 5.15 h, 41 keywords, on the box:

| | chunk16 (320 ms) | chunk8 (160 ms) |
|---|---|---|
| decode lag Δ, TP-weighted | — | **−101 ms** (median −75) |
| recall *excl.* `panther lake` | **0.842** | **0.789** |
| TP / miss | 48 / 9 | 45 / 12 |
| FA/h | 15.0 | 14.4 |

Headline recall is identical (0.818 both) but that is an artifact: `panther lake` carries
2,354 false alarms and its +3 TP masks losses. `sikorsky` went **2/2 → 0/2**; `security` 7→6.

**Verdict: FAIL against the pre-registered "−160 ms at unchanged recall" bar. Do not promote.**
Chunk-8 is below the trained 16/32/64 grid and the regression is exactly where that predicts.
Caveat: thin sample — 63 TPs, and `sikorsky` is n=2.

### 2d. Box repaired

`scripts/box-ready.sh --repair` → deployed `0f91959`, release build OK (38.5 s, binary sha256
`8857ebae…`). **Blockers 5 → 1.** Gated on the test suite first (212 pass; the single failure
was the doc-lint test, red only from another session's untracked scripts, absent from HEAD).

---

## 3. Files changed

| File | Change | Why |
|---|---|---|
| `src/bench/mod.rs` | `BenchArgs.model_dirs`; third loop level in `run_bench`; `apply_model_dir()` with refusal semantics; `GridCell.model_dir` (full path); `model_label()` with tail-elision; `model` table column shown only when >1 model swept; 5 unit tests | The interleaved comparison P1 needed. Empty `model_dirs` reproduces the previous grid and table byte-for-byte |
| `src/main.rs` | `--model-dir` arg (comma-separated `Vec<PathBuf>`) + wiring; fixed `--engines` help text which omitted `streaming_ctc` | CLI surface for the above. The help text was stale and would mislead someone running exactly this bench |
| `docs/BAKEOFF.md` | Added §"P1 RESULT" (full numbers, verdict, caveats); §"P1 IS MISLABELLED"; §"How to run P1" with the exact invocation | Protocol doc and the place a bake-off result must be pinned |
| `docs/nodes/evidence-benchmark.md` | Input contract gains the model-dir list + refusal semantics; output contract documents `GridCell.model_dir` | Repo rule: a node change is not done until its contract doc matches the code |
| `docs/CTC-MODEL-SOURCING.md` | `chunk 16` → `--chunk-size 8`, with a boxed correction deriving `decode_chunk_len = chunk_size × 2` and the `T=29` acceptance check | The wrong label would have produced the already-armed artifact |
| `docs/RACE-CHECKLIST.md` | P1 checked off with the negative verdict and both corrections | Closes a follow-on open since 2026-07-21 |
| `docs/RACE-STRATEGY.md` | P1 ladder item relabelled chunk-16 → chunk-8 | Same correction |
| `architecture/claims.toml` | Retired `readiness.variant_study_log_count` (paths committed = terminal transition); added `bench.model_dir_grid` and `detector.quantum_label_correction`; extended `preflight.admission_gate` and `admission.measure` | doc-lint enforces one open claim per node and one claim per dirty governed path |
| `architecture/nodes.toml` | Registered `chiavellini.md` under `evidence.measure` | Root `.md` is governed; unregistered files fail doc-lint |

`src/main.rs` is owned by `runtime.assembly` and already covered by the open
`phone.runtime_assembly` claim, so it is deliberately **not** listed in any new claim — a path
may carry only one open claim.

---

## 4. Files created

| Path | Purpose |
|---|---|
| `chiavellini.md` | This handoff |
| `pm-expansion-exploration/latency/dualhost/README.md` | Recipe for the one experiment that matters next (§8). Design was **revised** mid-session: the original put a recorder on the second host, which collides with CLAUDE.md's "never point a recorder at a tradeable call" |
| `pm-expansion-exploration/latency/dualhost/compare_hosts.py` | Pairs `decision intent` lines across two hosts' `launch.log`s and reports the per-utterance wallclock delta. Self-tested: identical logs → exactly 0.0 ms; injected 250 ms skew → recovered +250.0 ms |
| `state/archive/tln-26aug05-phone/` | The TLN run's evidence: `report.json`, `launch.log`, `journal.jsonl`, `redial.log`, `ngrok.log`, `preflight-result.json`, and the config. Copy-only |
| `state/archive/p1-chunk8-26aug06/` | P1 bench evidence: `bench-p1-interleaved.json`, `bench-p1.log` |

Remote (us-east-1 box): deployed `0f91959` + rebuilt `target/release/earshot`. Transient
`/tmp/p1-run.sh`, `/tmp/p1.json`, `/tmp/p1.log` left in place.

---

## 5. Files reviewed but intentionally left untouched

- **`src/source/twilio_ws.rs`, `src/risk.rs`, `src/kalshi/executor.rs`, `src/pipeline/*`,
  `src/detector/ctc_scorer.rs`, `src/decision.rs`** — read closely for the budget; the
  investigation was read-only by instruction and nothing needed changing.
- **`scripts/launch-webcast.sh` + `tests/launch_webcast_posture.rs`** — mid-session the
  posture test failed because the script had been edited (W7 lift) without updating the test.
  Left alone: deciding whether the test or the script is right is a posture call in another
  lane. **The other session resolved it** (now 7/7 passing).
- **All `configs/*.toml`** — no config touched. In particular the armed model was **not**
  swapped to chunk-8, because it failed the gate.
- **`models/`** — gitignored; the chunk-8 export was benched in place on the box, not promoted
  or copied into the laptop's arming path.
- **`scripts/fetch-models.sh`** — would need a URL+sha pin for a self-produced export. Not
  touched because chunk-8 is not being promoted.
- **The other session's paths** (`src/kalshi/executor.rs` edits, `scripts/green-guardian.sh`,
  `night-before-sweep.sh`, `post-event-report.sh`, `posture-from-study.py`) — not mine, not
  claimed by me, not registered by me.

**No dialing, no orders, no arm, no probe.** Nothing outward-facing except the box deploy.

---

## 6. Source-of-truth documents

Treated as authoritative, in this order:

1. **`AGENTS.md`** — ownership protocol. Drove every claim decision: one node per change set,
   one open claim per node, claim before editing, retire on commit.
2. **`CLAUDE.md`** — safety conventions. Directly changed the experiment design (the
   never-point-a-recorder-at-a-tradeable-call rule) and supplied the command surface.
3. **`docs/STATUS.md`** — current-state authority; confirmed phone-lane coverage is the
   binding constraint, not plumbing.
4. **`architecture/nodes.toml` + `architecture/claims.toml`** — machine authority for
   ownership; `scripts/doc-lint.py` enforces both and was run as the gate.
5. **`VERIFICATIONS.md`** — pinned wire facts. The `decode_chunk_len=32 → 320 ms, T=45,
   out_per_chunk=8` pin is what let the chunk-16/chunk-8 confusion be resolved definitively.
6. **`docs/LATENCY-PROOF.md` (§E0c/E0d)** and **`docs/BAKEOFF.md` (§P3)** — existing measured
   refutations. Used to kill `chunk_ms` (byte-identical cells, same sha256), prefix-firing
   (~32 ms), and thread tuning as levers *without re-running anything*.
7. **`docs/COMPETITOR-ANATOMY.md`** — the pre-registered competitor model. The TLN measurement
   *confirms* its "Today: ~+1.0–1.3 s" row rather than contradicting it.

Primary data outranked all docs where they disagreed: the **Kalshi public trade tape**
(`api.elections.kalshi.com/trade-api/v2/markets/trades`, fields `yes_price_dollars` /
`count_fp`), the run's own `launch.log`/`report.json`, the ONNX metadata on disk, and
upstream `export-onnx-streaming-ctc.py` (`decode_chunk_len = model.chunk_size * 2`).

---

## 7. Current project state

**Different from the start:**

- The phone-lane latency budget is measured, not inferred. 371/534 ms is ours.
- `bench-detector` can compare models in one run; 5 new tests; contract docs match the code.
- P1 is closed with a negative verdict and archived evidence.
- The chunk-16/160 ms error is corrected in all four docs.
- The box is deployed at `0f91959` and down to one blocker.

**Uncertainty and incomplete work — read this before continuing:**

1. **Nothing is committed.** All of the above is uncommitted working-tree state.
2. **A second agent session was editing this same tree throughout.** It committed
   `881ac73` mid-session, which **swept this session's `src/bench/mod.rs` and `src/main.rs`
   work into a commit whose message is about something else** ("session 26AUG05: 10 events,
   first real fills…"). The code is in HEAD and correct; its provenance is not.
3. **`doc-lint` currently exits 1** — solely on four untracked scripts belonging to the other
   session (`green-guardian.sh`, `night-before-sweep.sh`, `post-event-report.sh`,
   `posture-from-study.py`): no owning node, no claim. Nothing from this session errors.
4. **The upstream leg is still NOT MEASURED**, and the arithmetic says it is now decisive
   (§8).
5. **This session degraded a live run.** Three parallel investigation agents drove the host to
   load 100 on 8 cores while the TLN phone arm was live; drops spiked 4,007 in two minutes.
   Agents were killed at 21:20Z. Do not run fan-out investigation against a live event host.

---

## 8. Immediate next steps

**The arithmetic that should drive prioritisation.** From the tape: to place an order at all
(97¢ cap) we must be **340 ms** faster; for real edge (≤75¢, 238 contracts available)
**430 ms**.

| Stack | Deficit | Fills? |
|---|---|---|
| today | 534 ms | no |
| + P1 chunk-8 (−101 measured) | 433 ms | no |
| + 80 ms-quantum family (−240 est.) | 294 ms | no |
| + host move (−280 est. combined) | 254 ms | no |
| **needed** | **≤194 ms** | — |

**No detector-only path reaches a fillable price.** The upstream leg is therefore not
optional.

Prioritised:

1. **Decide the provenance problem** (blocking, minutes). This session's work is inside
   `881ac73` under an unrelated message. Either amend/annotate, or accept and note it. Also
   settle whether the other session's four untracked scripts get registered — `doc-lint` and
   `cargo test --test architecture_contract` stay red until they do.
2. **Run the one experiment** — `pm-expansion-exploration/latency/dualhost/README.md`. Dual
   `arm` on the same bridge (box = real posture, laptop = non-crossing probe), compare
   `decision intent` wallclock. It measures the only unmeasured *differential* term and needs
   no new code. **Blocked on bridge coverage:** all four `held` venues (CMG, SPOT, PGR, TLN)
   have already reported. **WEN and TTWO (2026-08-07)** have hunted, unprobed numbers in
   `state/hunt/numbers.env` — probe them (`bridge-probe.sh --promote`); a `held` verdict makes
   one of them the venue.
3. **Clear the last box blocker** before any phone event runs there: Caddy does not route
   `/e/8093`. Either rebind to a routed port (8080–8085, 8099) or add the route to
   `deploy/caddy/Caddyfile` and re-run `scripts/install-tls-ingress.sh`. Re-run
   `scripts/box-ready.sh` until it exits 0 — **at prep time, not T-60**.
4. **Do not pursue quantum reduction on this checkpoint.** P1 is answered. If latency work
   continues, scope the G2 shortlist (Parakeet-Realtime-EOU-120M-v1, 80 ms, natively trained
   for it) behind the `Detector` trait — note it is RNN-T, so `ctc_scorer` does not apply and
   a new decode path is required.
5. **Two one-line fixes that would have saved this session:** log the NTP offset into
   `journal.jsonl` at arm start (the entire clock caveat disappears), and make `model_lag` a
   first-class telemetry edge (`edges{}` currently understates in-process latency ~4× and that
   is exactly the error this investigation started from).
6. **Fix `lost_races` on fanout runs** — reports 0 while individual order records say
   otherwise. Until then, count order records, never the aggregate.

### Commands to re-verify this session's work

```
export ORT_DYLIB_PATH="$PWD/vendor/onnxruntime/onnxruntime-osx-arm64-1.27.1/lib/libonnxruntime.1.27.1.dylib"
cargo test --release --lib bench::         # 13 pass, incl. 5 new
cargo test --test bench_fixture            # 2 pass
scripts/doc-lint.sh                        # red ONLY on the other session's 4 scripts
scripts/box-ready.sh --config <cfg> --account ~/.earshot/<a>.env   # expect 1 blocker (/e/8093)
```

Never pipe a gate through `tail`/`tee` — it masks `$?`. Remote bench runs must export
`ORT_DYLIB_PATH` explicitly; non-interactive ssh does not inherit it and the detector then
hangs silently at zero CPU.
