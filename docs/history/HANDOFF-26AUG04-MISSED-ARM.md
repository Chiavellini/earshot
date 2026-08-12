# HANDOFF — the 2026-08-04 SPOT/PGR miss, and the four fixes that closed it

**Session date:** 2026-08-04 (started ~14:50Z, on the laptop, branch
`webcast-lane`).
**Trigger:** operator reported that Spotify and Progressive "failed" that
morning — *"we still keep dropping the line; we still fail to sustain a
connection."*
**Outcome:** that diagnosis was wrong, the real cause was three separate node
faults, all four approved fixes are landed and verified.

Read `docs/history/POSTMORTEM-26AUG04.md` first for the full evidence. This file is the
operational handoff: what changed, what did not, and what to do next.

---

## 1. Initial project state

`git status` at session start showed a working tree already mid-flight from a
previous session (the PLTR/SNAP/GRAB 26AUG03 work):

```
M architecture/claims.toml     M scripts/launch-event.sh
M architecture/nodes.toml      M scripts/launch-webcast.sh
M configs/dial-ins.toml        M scripts/preflight-event.sh
                               M scripts/run-arm.sh
                               M scripts/variant-study.sh
+ ~55 untracked files (configs, corpus artifacts, 5 new scripts, POSTMORTEM-26AUG03.md)
```

HEAD was `41d1bcc` ("hunt: auto 2026-08-04").

### What was actually wrong when I arrived

1. **`scripts/doc-lint.sh` was FAILING with 35 errors.** Three scripts had **no
   owning node** — `chain-capture.sh`, `stage-afternoon-26aug04.py`,
   `study-afternoon-26aug04.sh` — plus unclaimed dirty governed paths. Since
   the project's whole premise is that a modular node registry localizes
   faults, and the morning's runtime path (`chain-capture.sh`) sat *outside*
   that registry, the failure was structurally unattributable. This is the
   single most important pre-existing condition.
2. **Zero detections / decisions / orders existed for 2026-08-04.** The only
   state written that day was `state/chain/*.sid` files. No
   `state/KXEARNINGSMENTIONSPOT-*` or `-PGR-*` run dir existed at all.
3. **`configs/dial-ins.toml` had no `[SPOT]` and no `[PGR]` section**, despite
   both bridges having admitted us that morning.
4. **`slate/latest.json` did not contain a PGR event for 2026-08-04** — only a
   stale `KXEARNINGSMENTIONPGR-26JUL15`.
5. `com.earshot.pgr-chain` (pid 9336) was **still recording Progressive** when
   the session began; it completed on schedule at 15:05Z during the session.

### The misleading part

The evidence *looked* like a telephony problem (every leg ending at ~598 s,
hundreds of "stalls"), which is why the reported symptom was "dropped line". It
was not.

---

## 2. Work completed

### The diagnosis (this is the load-bearing part — do not re-derive it)

**The line never dropped and the phone lane never failed. It was never invited.**

- **Frame count matches wall-clock in every leg** (598.3 s of audio across a
  598.1 s span). No audio was lost. The "91–300 stalls" are *arrival jitter*
  (bursty delivery), not gaps in the call.
- **The Spotify transcript is clean and complete** — operator preamble, Brian
  Goldberg, both co-CEOs, Slido instructions. DTMF admitted on the first try.
  It stops mid-sentence at 598 s, before Q&A.
- **The terminator is our own log line**, on every leg:
  `WARN earshot::capture_bakeoff: capture bake-off: max duration reached —
  stopping max_secs=600` (`src/capture_bakeoff/mod.rs:239,252`).

**The 600 is not in the Rust.** `max_secs` is a CLI `u64` with no upper bound
and a Rust default of 120. The 600 came from `scripts/chain-capture.sh:19`
(`SECS=600`). `scripts/bridge-probe.sh:46` refuses `--secs > 600` *correctly* —
"a probe listens to the entry flow, not the call". The defect was using a probe
for an event.

`bridge-probe` (`src/main.rs:699-708`) builds a bare `TwilioWsSource` and hands
it to `run_bridge_probe`: **no config, no detector, no DecisionTable, no
RiskGate, no Kalshi**, and `max_reconnects = 0`. The `arm` path
(`Command::Arm`, `src/main.rs:349` → `arm()` at `:1053` → `run_pipeline`) has
**no wall-clock cap anywhere**, and `launch-event.sh` never passes
`--pause-secs`, so a production dial holds one call for 90 min
(`<Pause length="5400"/>`). Pinned: CMG held **4,572 s on one CallSid** with 4
detections.

Cost, measured from the public Kalshi API: SPOT 18 markets / **157,217
contracts**, PGR 12 markets / **~178,000 contracts**, both repricing to
settlement during calls we were recording to WAV.

### The three node faults

| Node | Fault |
|---|---|
| `control.discover` | Kalshi reuses ONE event ticker per series for the standing "next earnings call" and never re-dates `sub_title`. `KXEARNINGSMENTIONPGR-26JUL15` read "On Jul 15, 2026" on Aug 4 while its board traded 14:33–14:54Z. The worklist filter `today <= call_date` (`discover-events.py:603`) dropped it, so **Progressive was never on the slate at all**. `ticker_misleads()` is structurally blind here — ticker and stale `sub_title` are wrong *together* and therefore agree (the HOOD shape). |
| `control.qualify` | A working SPOT number + PIN admitted us to the live call; the proof existed only in a **gitignored transcript header**, because `bridge-probe.sh` ended by *printing a paste-ready snippet* for a human. Nobody pasted it. With no `[SPOT]` section, `slate-stage.sh:291` (`PHONE_OK=1` iff `admission == "held"`) staged webcast-only and no phone arm was ever derived. |
| governance | `doc-lint` failing; `chain-capture.sh` unowned while straddling three nodes (drives `earshot bridge-probe`, calls `twilio-dial.sh`/`twilio-hangup.sh`, writes `corpus/bridge-probes/*`). `docs/ARCHITECTURE.md:46-47` calls exactly this a contract change. |

### A second defect found while reading the tape

`chain-capture.sh` ran **24 legs, not 6**. Progressive's call ended during leg 6
(529.9 s, short of the cap); legs 07–23 then held **~12.3 s each** — dial, hear
"the conference has ended", hang up, redial — **17 wasted paid dials**. Wall
clock cannot distinguish "still capturing" from "dialling a corpse". This is the
same principle `docs/nodes/control-supervise.md` already states as *a dial is
not a capture*; the recorder never implemented it.

### Decisions the operator made during the session

1. Tonight's four events (SPCX/ZETA 20:30Z, AMD/TOST 21:00Z) run
   **webcast-only**; the session's work is the durable pipeline fix.
2. Production telephony moves back to the **box + TLS ingress**
   (`--tls-host media.3-212-174-94.sslip.io`).
3. **All four** faults get fixed (discover, qualify, governance, reconciliation).

---

## 3. Files changed

⚠ **Three of these were ALREADY modified when the session began** (previous
session's work). I added to them; I did not author their earlier hunks.

| File | Δ | What changed, and why |
|---|---|---|
| `scripts/discover-events.py` | +103/-2 | **New `stale_open_event()` detector.** An event returned by the `status=open` sweep whose call date has passed is misdated, not over — both sweeps query `status=open`, so the pair is self-refuting and free to detect. Routed through the **existing** DISPUTED escape hatch (`:605`) so it escapes the `today <= call_date` filter; stale events now always earn the issuer-page confirmation fetch. Frozen **PGR fixture** added to `--self-test` beside BA and HOOD, plus 4 cry-wolf guards. Module docstring documents the third misdating class. **Deliberately NOT a date source** — `call_date()`'s "NEVER from the ticker" rule (`:365`) is untouched. |
| `scripts/recon-worklist.py` | +55/-7 | Same structural bug lived here at `:63`. A stale event's date is in the *past*, so HOOD's substitution rescue cannot reach it — it now **bypasses the horizon outright** and emits `STALE-DATE(open, kalshi=…)`. ESCALATE wording made honest for these rows ("BOARD IS OPEN but its only date … has passed") instead of the nonsensical "calls in -20d". PGR case + a "genuinely past events stay dropped" guard added to `--check`. |
| `scripts/bridge-probe.sh` | +80/-2 | **`--promote`** (+ `--issuer`, `--corpus-only`): on a clean hold, writes the verdict via `registry-admit.py` instead of printing a snippet. Only `held` auto-writes (leg ran to `--secs` within 5%); short holds print human-classification instructions. **Long-capture guard**: `--secs > 300` against an open board is refused — short admission probes on event day are explicitly still allowed, since that is the normal workflow. |
| `scripts/chain-capture.sh` (untracked) | ~+75 | **Refuse-to-record guard** (`--corpus-only` override, fail-CLOSED on an undetermined board lookup, exit 6/7). **Circuit breaker**: stops after `--max-short` (default 3) consecutive legs under 10% of requested duration — the 17-wasted-dials fix. |
| `scripts/slate-stage.sh` | +11/-1 | **`--issuer TICKER`** single-event restage, so a morning probe that flips admission to `held` can still reach a phone arm the same day. The existing idempotence already handles "webcast exists, phone does not", which is exactly a freshly-promoted venue's state. The derive block (`:484-525`) is **unchanged**. |
| `architecture/nodes.toml` ⚠ | +42/-0 | Owners assigned: `chain-capture.sh` → `evidence.capture`; `stage-afternoon-26aug04.py` → `control.prepare`; `study-afternoon-26aug04.sh` → `evidence.benchmark`; `registry-admit.py` → `control.admission`; `board-open.py` → `control.qualify`; `missed-arm-check.py` → `control.grade`; `POSTMORTEM-26AUG04.md` → `evidence.measure`. `configs/.acks/**` added to `excluded_globs` (generated per-event operator records, same class as `.readiness`). |
| `architecture/claims.toml` ⚠ | +208/-1 | Extended five existing claims (doc-lint permits one open claim per node) and opened exactly one new one, `missed_arm.reconcile` on `control.grade`. Also claims the 50 unclaimed `corpus/bridge-probes/*` artifacts and the 9 unclaimed 26AUG04 configs. |
| `configs/dial-ins.toml` ⚠ | +77/-0 | **`[SPOT]` and `[PGR]` backfilled** from the morning's own probes (the other four new sections — SNAP/GRAB/PLTR/AMD — are from the previous session). `held` coverage **1 → 3 venues**. Digits are NOT here by policy. |
| `VERIFICATIONS.md` | +74/-0 | **Q-STALE-EVENT** pinned (one ticker per series, stale `sub_title`, with the trade timestamps as evidence) and a **Q-ADMIT addendum** for SPOT/PGR hold times. |
| `CLAUDE.md` | +11/-1 | New non-negotiable convention: *never point a recorder at a tradeable call*. New quick commands (`--promote`, `--issuer`, `missed-arm-check.py`, `board-open.py`). |
| `docs/STATUS.md` | +14/-4 | The "exactly one is `held`" line was now false. Updated to 10-of-64 with admission, **three** `held`, plus how a probe's verdict now reaches the registry. |
| `docs/work/ACTIVE.md` | +8/-3 | Claim count 20 → 23; `readiness-lane` row added; notes the extend-don't-open-new pattern. |
| `docs/nodes/control-discover.md` | +30 | Contract now states the `status=open` invariant is load-bearing and documents `stale_open_event` as an untrustworthiness marker, never a date source. |
| `docs/nodes/control-qualify.md` | +36 | Documents `STALE-DATE` bypassing the horizon, `board-open.py` ownership, and **the one writer outside this node** (`registry-admit.py`) with the cycle-avoidance reasoning. |
| `docs/nodes/control-admission.md` | +30/-3 | Documents `--promote`, the narrow auto-promotion rule, proven-digit persistence, and the long-capture guard. |
| `docs/nodes/evidence-capture.md` | +24 | *"Does not launch a traded arm"* is now **enforced, not merely stated**; documents `chain-capture.sh` ownership and the short-leg breaker. |
| `docs/nodes/control-grade.md` | +26/-1 | Documents missed-arm reconciliation, why it keys on **traded** (not open) boards, and its two cry-wolf guards. |

---

## 4. Files created

| File | Lines | Purpose |
|---|---|---|
| `scripts/registry-admit.py` | 376 | Writes a probe's own admission verdict into `configs/dial-ins.toml`. **Comment-preserving by construction** — surgical line edits, never `tomllib` parse-and-dump, because that file's rules and per-venue history are load-bearing docs a round-trip would delete. Section splitting is `"""`-aware (`[HOOD]`'s notes contain bracketed prose). Also upserts proven digits. `--check` self-test covers comment preservation, idempotence, section creation, quote escaping, `last_verified` refresh. |
| `scripts/board-open.py` | 192 | "Does this issuer have an open Kalshi mention board?" — one public unauthenticated GET. **Two consumers share one implementation**: the refuse-to-record guard and missed-arm reconciliation. Exit 0/1/2 where **2 = UNDETERMINED** so callers can fail closed. `--any-status` answers the retrospective form (a board settles). |
| `scripts/missed-arm-check.py` | 246 | The standing detector for this failure class: *captured audio + traded board + zero orders = `MISSED-ARM`*. Deliberately **independent of which upstream node broke**. Two load-bearing guards: legs < 60 s are not coverage, and `state/` run dirs are date-filtered by event-ticker suffix. |
| `docs/history/POSTMORTEM-26AUG04.md` | 131 | Full evidence-backed account. |
| `state/hunt/numbers-probed.env` | — | **Gitignored, 0600.** Probe-proven digits for SPOT and PGR, distinct from the hunt-regenerated `numbers.env` (whose header says "regenerate, never hand-edit"). Without this, `admission = "held"` names a bridge nobody can dial. |

---

## 5. Files intentionally left untouched

| File / area | Why |
|---|---|
| `scripts/launch-event.sh`, `launch-webcast.sh`, `preflight-event.sh`, `run-arm.sh`, `variant-study.sh` | **Already modified when the session began** (previous session). Reviewed in depth — `launch-event.sh`'s redial supervisor and 90-min `<Pause>` are the *correct* behaviour and the reason the arm path was never at fault. Touching them would have entangled two sessions' work. |
| `src/capture_bakeoff/mod.rs` | The 600 s cap is **not** here; `max_secs` is an unbounded CLI arg with a Rust default of 120. Raising a nonexistent constant would have been the wrong fix. |
| `src/source/twilio_ws.rs`, `src/main.rs`, all Rust | **The runtime performed to contract wherever it was exercised.** Every leg logged signature-verified → connected → stream started → held. No Rust change was warranted; changing it would have been fixing a working component. |
| `scripts/slate-stage.sh:484-525` (phone derive) | The generator is **correct** — it already emits the Q-REDIAL production values (`max_reconnects = 2`, `idle_timeout_secs = 8`). It simply never ran, because the admission gate blocked it. Early suspicion here was wrong and is recorded as such. |
| `src/risk.rs`, kill-switch scope, 1¢ posture, listen-only TwiML invariant | Out of scope and explicitly protected by `CLAUDE.md`. |
| `call_date()`'s "NEVER from the ticker" rule | A deliberate, load-bearing rule from the BA case. The fix adds a *contradiction detector*, not a second date source. |
| `configs/*-autorun.env` `TO_NUMBER="TBD"` | A deliberate **fail-closed** default (`event-autorun.sh` fail-stops on TBD) because digits rotate per call. Auto-filling it would silently reverse a safety decision. Proven digits are now persisted and discoverable, but a human still fills the field. |
| launchd plists | `com.earshot.pgr-chain` was left to finish on its own (its board had already settled). No scheduled job was added — unattended paths need a launchd-fired rehearsal per `CLAUDE.md`. |

---

## 6. Source-of-truth documents

Treated as authoritative, in this order:

1. **`CLAUDE.md`** — the non-negotiable conventions. Three governed this session
   directly: *a node change is not done until its contract doc matches the
   code* (hence the five `docs/nodes/*.md` edits); *Kalshi API facts live in
   `VERIFICATIONS.md`, pinned with evidence in the same change* (hence
   Q-STALE-EVENT); and the **rehearsal rule** for unattended paths (hence no
   new launchd job).
2. **`architecture/nodes.toml` + `architecture/claims.toml`** — the machine
   authority for ownership. `docs/work/ACTIVE.md:8` states plainly: *"If this
   page and the TOML disagree, stop and fix the TOML."* `scripts/doc-lint.sh`
   is the executable form of this and was the primary success gate.
3. **`VERIFICATIONS.md`** — dated live pins. Q-TLS (ngrok p99 226 ms / 61
   stalls vs TLS-on-box 20.1 ms / **0 stalls**), Q-REDIAL (drop → audio 4.03 s),
   Q-ADMIT (THE MSFT RULE), Q-NGROK. These made the ingress and redial
   arguments *measured* rather than asserted.
4. **`docs/STATUS.md`** — named the binding constraint ("exactly one is
   `held`"), which is what made the SPOT registry gap the highest-value fix.
5. **`docs/history/POSTMORTEM-26AUG03.md`** — the immediately preceding session; its
   shape (measure, then state what is NOT resolvable) is the model
   `POSTMORTEM-26AUG04.md` follows.
6. **`PHONE-LANE-HANDOFF.md`** — read with care: it self-declares **HISTORICAL
   as of 2026-08-01, its state is WRONG**. Used only for the *reasoning*
   (why the phone lane is worth 4–8 s), never as status.
7. **Primary evidence over all documents**: `state/chain/pgr-2026-08-04.log`,
   `corpus/bridge-probes/*.json[l]`, the Spotify transcript, and read-only GETs
   against the public Kalshi API. Where docs and artifacts disagreed, artifacts
   won — that is how the "dropped line" premise was falsified.

---

## 7. Current project state

**Different from the start:**

- `scripts/doc-lint.sh`: **FAILED (35 errors) → OK**, 0 dependency cycles, 479
  governed files, 23 open claims. Every script has an owning node, so the next
  failure of this class *is* attributable to a node.
- `cargo test`: **24 suites, 290 passed, 0 failed** (verified with a true exit
  code — the first run was piped through `tail`, which masked cargo's status).
- Probed `held` admission coverage: **1 venue → 3** (CMG, SPOT, PGR).
- Two previously-invisible events are now visible: PGR (retrospectively) and
  **BULL / Webull** — found unprompted on the fix's first live run, 17 markets
  open and **unresolved** (0.15–0.58), i.e. a call that has not happened yet.
- A recorder can no longer be silently pointed at a tradeable call.
- The failure class now has a standing detector.

**Remaining uncertainty / incomplete:**

- ⚠ **The live `--promote` path has never placed a real call.** Its components
  are individually proven (`registry-admit.py` verified against the real
  62-section registry: 62 preserved, comments and `[HOOD]`'s multi-line notes
  intact; the SPOT/PGR backfills are real writes). The *wiring* inside
  `bridge-probe.sh` is syntax-checked only. This is the one gap.
- `missed-arm-check.py` is **not wired into any scheduled job**.
- The ingress regression is documented, not fixed in code: today ran on the
  laptop through ngrok while the box sat up with Caddy active. **Honest
  caveat**: SPOT via ngrok showed only 11 stalls today, so ngrok is *not* the
  sole cause of PGR's 300 — per-bridge variance is real. TLS-on-box is still
  the measured-better ingress and free to use.
- **Nothing is committed.** All work is in the working tree on `webcast-lane`,
  intermixed with the previous session's uncommitted changes.
- `docs/DIAL-INS.md:158` still lists PGR as "not researched" — stale now.

---

## 8. Immediate next steps

**Priority 1 — tonight's events (SPCX/ZETA 20:30Z, AMD/TOST 21:00Z), webcast-only.**
Launch on the box through TLS, per the operator's decision:

```bash
ssh -i ~/.ssh/earshot-useast1.pem ubuntu@3.212.174.94   # verified up, caddy active
scripts/preflight-event.sh --config configs/webcast-amd-26aug04-arm-wc.toml
scripts/launch-webcast.sh --config <cfg> --tls-host media.3-212-174-94.sslip.io
```
Confirm `scripts/study-afternoon-26aug04.sh` finished — at session end it was
still running (`spcx zeta amd tost`), and `preflight-event.sh:92` hard-FAILs on
a missing `configs/.readiness/*.json`.

**Priority 2 — run the reconciliation after every event tonight.** This is the
new habit the whole session exists to create:
```bash
python3 scripts/missed-arm-check.py --date 2026-08-04
```

**Priority 3 — close the one unproven path** (~$0.02, needs operator go-ahead
since it places a real call):
```bash
scripts/bridge-probe.sh --to +17627212978 --label ttsrehearsal-2026-08-04 \
    --secs 120 --promote --issuer TTSREHEARSAL
# then confirm dial-ins.toml gained a correct section with no hand-editing,
# and revert that test section before committing.
```

**Priority 4 — chase the two newly-visible boards.**
`BULL` (Webull) is unresolved and upcoming — probe its bridge and, if held,
`scripts/bridge-probe.sh … --promote && scripts/slate-stage.sh --issuer BULL`.
Re-derive PGR's real call cadence (Progressive reports monthly, which is why
its ticker rolls).

**Priority 5 — commit.** Nothing is committed and two sessions' work is
intermixed. Suggested split: (a) governance + ownership
(`architecture/*`, `doc-lint` green), (b) the four fixes + their contract docs,
(c) `VERIFICATIONS.md` pins + `POSTMORTEM-26AUG04.md`. Gate every commit on:
```bash
scripts/doc-lint.sh && cargo test
python3 scripts/discover-events.py --self-test
python3 scripts/recon-worklist.py --check
python3 scripts/registry-admit.py --check
python3 scripts/board-open.py --check
python3 scripts/missed-arm-check.py --check
```

**Priority 6 — deferred, needs a rehearsal.** Wire `missed-arm-check.py` into
`slate-daily.sh` or its own launchd job. Per `CLAUDE.md` this counts as
validated only when **launchd-fired**, killed with a real SIGTERM, under
launchd's minimal env — not a manual run.

**Priority 7 — housekeeping.** Update `docs/DIAL-INS.md:158` (PGR is no longer
"not researched"). Consider whether `chain-capture.sh` should survive at all now
that the guard exists — its only real justification was working around
`bridge-probe.sh`'s inline ~6-minute transcription, which is what cost Spotify
everything after minute 10; deferring transcription to after the last leg would
remove the need for leg-chaining entirely.

---

## Traps for the next agent

1. **Do not "fix" the 600 s cap in Rust.** It is not there.
   `scripts/bridge-probe.sh:46`'s `1..600` refusal is *correct* for a probe.
2. **Do not make `control.qualify` consume `control.event.admitted`.** It closes
   a `qualify → admission → qualify` cycle that `doc-lint` rejects. That is why
   `registry-admit.py` is owned by `control.admission`.
3. **Never `tomllib`-round-trip `configs/dial-ins.toml`.** Its comments are
   load-bearing documentation (THE MSFT RULE, THE APPLE RULE, per-venue
   history). Use `registry-admit.py`.
4. **`ticker_misleads()` cannot catch the PGR class** — ticker and stale
   `sub_title` agree. Do not "simplify" the two detectors into one.
5. **Stalls ≠ lost audio.** `stalls_over_100ms` counts arrival-gap intervals.
   Compare `frames × 20 ms` against wall-clock before concluding loss.
6. **A short bridge-probe against an open board is normal and necessary** —
   that is how admission is established on event day. Only >300 s is refused.
