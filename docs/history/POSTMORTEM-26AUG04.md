# Post-mortem — SPOT + PGR, 2026-08-04

Spotify (12:00Z) and Progressive (~14:00Z) were the two events we needed to be
precise on. Both produced **zero detections, zero decisions, zero orders**.

The reported symptom was "we keep dropping the line, we can't sustain a
connection." That is not what happened, and the difference matters because it
points at an entirely different set of nodes.

**The line never dropped. The phone lane never failed. It was never invited.**

| | Spotify | Progressive |
|---|---|---|
| what ran | `earshot bridge-probe` (record-only) | `chain-capture.sh` → same, 24 legs |
| detector / orders | none / none | none / none |
| audio quality | **11 stalls / 598 s — clean** | 91–300 stalls/leg (jitter, not loss) |
| coverage | **10 min of a 67 min call** | 6 full legs, then 17 dead re-dials |
| ended by | our own 600 s cap | our own 600 s cap, ×6 |
| board | 18 markets, **157,217 contracts** | 12 markets, **~178,000 contracts** |

## The evidence against "dropped line"

1. **Frame count matches wall-clock in every leg** (598.3 s of audio across a
   598.1 s span). No audio was lost. The "300 stalls" are *arrival jitter* —
   bursty delivery — not gaps in the call.
2. **The Spotify transcript is clean and complete**: operator preamble, Brian
   Goldberg, both co-CEOs, the Slido instructions. Admission by DTMF succeeded
   on the first try. It stops mid-sentence at 598 s, before Q&A — where most
   mention markets resolve.
3. **The terminator is our own log line**, on every leg:
   `WARN earshot::capture_bakeoff: capture bake-off: max duration reached —
   stopping max_secs=600` (`src/capture_bakeoff/mod.rs:239,252`).

The 600 is not in the Rust. `max_secs` is a CLI `u64` with no upper bound and a
default of 120; the 600 came from `scripts/chain-capture.sh:19`.
`bridge-probe.sh:46` refuses `--secs > 600` **correctly** — "a probe listens to
the entry flow, not the call". The defect was using a probe for an event.

The real path has none of these limits. `Command::Arm` → `run_pipeline` has no
wall-clock cap anywhere, and `launch-event.sh` never passes `--pause-secs`, so
a production dial holds one call for 90 minutes (`<Pause length="5400"/>`).
Pinned: CMG held **4,572 s on a single CallSid** with 4 detections. Production
phone configs carry `max_reconnects = 2` / `idle_timeout_secs = 8`, recovering
a real drop in 4.03 s. `bridge-probe` hardcodes `max_reconnects = 0`.

Every hardening this project has built was bypassed, because a different tool
ran.

## Three independent node faults

**PGR — `control.discover`.** Kalshi reuses one event ticker per series for the
standing "next earnings call" and never re-dates `sub_title`.
`KXEARNINGSMENTIONPGR-26JUL15` still read "On Jul 15, 2026" on Aug 4 while its
board traded 14:33–14:54Z. `discover-events.py:603` (`today <= call_date`)
dropped it, so **Progressive was never on the slate at all**. Pinned as
Q-STALE-EVENT. `ticker_misleads()` is structurally blind here — ticker and
stale sub_title agree, the HOOD shape.

**SPOT — `control.qualify`.** A working number + PIN admitted us to the live
call. That proof existed only in a gitignored transcript header, because
`bridge-probe.sh` ended by *printing a snippet for a human to paste*. Nobody
pasted it. With no `[SPOT]` in `configs/dial-ins.toml`, `slate-stage.sh:291`
(`PHONE_OK=1` iff `admission == "held"`) staged webcast-only. Per
`docs/STATUS.md`, `admission` was set on 6 of 58 venues and exactly **one** was
`held` — the binding constraint on the entire phone lane.

**Both — governance.** `scripts/doc-lint.sh` was FAILING with 35 errors,
including `governed first-party file has no owning node:
scripts/chain-capture.sh`. That script straddles three nodes at once. **This is
why the fault could not be localized to a node: the runtime path was outside
the architecture.**

## Fixes landed

1. **`control.discover`** — `stale_open_event`: an event returned by the
   `status=open` sweep whose call date has passed is misdated, not over. Routed
   through the existing DISPUTED escape hatch so it bypasses the horizon
   filter; `STALE-DATE` on the worklist; frozen PGR fixture in `--self-test`
   alongside BA and HOOD, plus four cry-wolf guards.
2. **`control.qualify` / `control.admission`** — `bridge-probe.sh --promote`
   writes its own verdict via the new `scripts/registry-admit.py`
   (comment-preserving surgical edits, never parse-and-dump), and persists the
   proven digits to `state/hunt/numbers-probed.env`. Only `held` auto-writes.
   `slate-stage.sh --issuer X` re-stages one event same-day.
   **`held` coverage: 1 venue → 3.**
3. **Governance** — every script has an owning node; `doc-lint` **passes** (0
   dependency cycles). Record-only tools now REFUSE against an open Kalshi
   board unless `--corpus-only`, failing closed on an undetermined lookup.
   `chain-capture.sh` stops after 3 consecutive short legs.
4. **`control.grade`** — `scripts/missed-arm-check.py`: captured audio + traded
   board + zero orders = `MISSED-ARM`. Independent of which node broke.

## A second defect found while reading the tape

`chain-capture.sh` ran **24 legs, not 6**. Progressive's call ended during leg
6 (529.9 s, short of the cap); legs 07–23 then held **~12.3 s each** — dial,
hear "the conference has ended", hang up, redial — 17 times. Wall clock cannot
distinguish "still capturing" from "dialling a corpse". This is the same
principle `docs/nodes/control-supervise.md` already states as *a dial is not a
capture*; the recorder simply never implemented it. Now bounded by
`--max-short` (default 3).

## What the fix found on its first run

The live sweep flagged a **second** blind event unprompted: `BULL` (Webull),
`KXEARNINGSMENTIONBULL-26JUL24`, 17 markets open and **unresolved** (0.15–0.58)
— a call that has not happened yet and that we had no visibility into. Only 2
of ~45 events flagged, so the rule is narrow rather than noisy.

## Not implicated

Detector and keyword quality. Today produced no detections at all because no
detector was ever attached; the `confirmed <= 2` follow-up from the 26AUG03
post-mortem is a separate lane. Also unchanged: `src/risk.rs` caps, kill-switch
scope, the 1¢ posture, the listen-only TwiML invariant, and `call_date()`'s
"never from the ticker" rule.

## Open

- **Ingress regression.** Today ran on the laptop through ngrok free tier while
  the box sat up with Caddy active. Q-TLS pinned the difference on the same
  number and tool: ngrok p99 226 ms / 61 stalls vs TLS-on-box p99 **20.1 ms /
  0 stalls**. Production telephony belongs on `--tls-host`. (Honest caveat:
  SPOT via ngrok showed only 11 stalls today, so ngrok is not the sole cause of
  PGR's 300 — per-bridge variance is real. The TLS path is still the
  measured-better ingress and costs nothing to use.)
- **`TO_NUMBER="TBD"` remains a deliberate fail-closed default** in the autorun
  env. Proven digits are now persisted and discoverable, but a human still
  fills that field; auto-filling it would reverse a safety decision made
  because digits rotate per call.
- `missed-arm-check.py` is not yet wired into a scheduled job.
