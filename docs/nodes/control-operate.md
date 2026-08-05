# control.operate — event-day operations and reconstruction

## Job

Give the operator one place to run an event day from and one place to
reconstruct it afterwards. Born 2026-08-04, the day all 7 events failed on
human-memory steps and the evidence of *why* was spread across five
filesystem roots with no index. This node owns the day runbooks, the
per-event lifecycle journal, the green gate shared by every launcher, the
descope record, and the installers that turn "a human remembers at T-minus"
into loaded launchd agents.

## Owns

- `docs/runbooks/**` — day-keyed operating procedure (RUNBOOK-YYYY-MM-DD.md);
  the historical accumulator `docs/RUNBOOK.md` stays with control.supervise.
- `scripts/journal.sh` — append-only per-event lifecycle journal writer:
  one JSON line `{ts,event,actor,action,outcome,detail,log,host,pid}` to
  `state/<EVENT_ID>/journal.jsonl`. ALWAYS exits 0 (a journaling failure must
  never kill a live event). Subprocess-invoked (the notify.sh pattern).
- `scripts/assert-ready.sh` — the green gate: exit 0 only when
  `state/<E>/preflight-result.json` is PASS + config-sha-matched + ≤24 h AND
  `configs/.readiness/<basename>.json` is hash-matched with no unacked
  `confirmed:0` keyword. `rehearsal-*` basenames exempt. Pure read.
- `scripts/descope-event.sh` — writes `state/<E>/DESCOPED.json` + pages; the
  record that makes a descope distinguishable from a miss (the MCD lesson).
- `scripts/watcher-liveness.sh` — page-only "nothing watches the watchers"
  check for due-but-dead arm-on-url watchers.
- `scripts/install-t0-agents.sh` — per-event launchd writer: watch +
  escalate (+ resolver) agents per webcast event, T−35 probe-reminder pages
  per non-held phone venue, the two liveness jobs. `--rehearsal` mode fires a
  5-minute drill.
- `scripts/study-nightly.sh` — date-driven variant-study runner in call
  order (generalizes the hand-written per-day study scripts).

## Does not own

- The launchers themselves (`control.launch`) — this node's gate is CALLED
  from them, additively; dial/redial/watchdog logic is untouched.
- `scripts/preflight-event.sh` and the verdict artifact it writes
  (`control.preflight` owns the gates; this node only consumes
  `preflight-result.json`).
- `configs/.readiness/**` (generated evidence, `evidence.benchmark` method)
  and `scripts/variant-study.sh` (its writer).
- `scripts/notify.sh` (`platform.notify`), `scripts/slate-stage.sh` /
  `scripts/slate-time-merge.py` (`control.prepare`), grading
  (`control.grade`).
- `state/**` content itself — journals are runtime evidence, ungoverned by
  design; this node owns the writer and the conventions, not the bytes.

## Input contracts

None registered yet. The de-facto inputs are files: `state/<E>/
preflight-result.json` (written by control.preflight since 2026-08-04),
`configs/.readiness/<basename>.json` (evidence.benchmark), staged configs
(control.prepare). They become typed doors in roadmap Phase D
(`preflight-result.json` is already shaped for it).

## Output contracts

None registered yet. `state/<E>/journal.jsonl` and `state/<E>/DESCOPED.json`
are the de-facto outputs, consumed by the missed-arm reconciliation
(control.grade) and the day-after reconstruction. Phase D types them as run
artifacts under `var/runs/<event>/<run-id>/`.

## Side effects and exclusive resources

- Appends to `state/<E>/journal.jsonl` (O_APPEND single-line writes; safe
  under concurrent actors).
- Writes `~/Library/LaunchAgents/com.earshot.<slug>-<datepart>-*.plist` and
  bootstraps them into `gui/<uid>` — the ONLY writer of per-event laptop
  agents; bootout the date's set after the event day.
- Pages via `scripts/notify.sh` (descope, gate refusal/override, dead
  watcher, study problems).
- Never places calls, never sends orders, never touches Twilio/Kalshi APIs.

## Failure contract

- `journal.sh` exits 0 unconditionally; callers append `|| true`.
- `assert-ready.sh` refusals are single-line `REFUSED: <why>` on stdout,
  exit 1; launchers page and exit 7 on refusal; `EARSHOT_GREEN_OVERRIDE`
  proceeds but pages ("an override that pages is a decision, not a bypass").
- `install-t0-agents.sh` prints `LOAD-FAILED:` per agent and exits 1 if any
  agent failed to bootstrap — a partially-installed day must be visible.
- `watcher-liveness.sh` pages the missing-slug diff and exits 1.
- `descope-event.sh` refuses to run without a reason string.

## Parallel work boundary

Safe alongside every other node: all writes are additive (journal appends,
new plists, new DESCOPED records). Do not edit launcher gate blocks and
`assert-ready.sh` semantics in separate parallel claims — the gate contract
spans both sides and must move in one change.

## Verification

- `bash -n` on every owned script.
- Journal round-trip: `scripts/journal.sh --event JOURNAL-SELFTEST --actor t
  --action note --outcome ok --detail 'x "y" <z>'` then `python3 -c
  "import json;[json.loads(l) for l in open('state/JOURNAL-SELFTEST/journal.jsonl')]"`.
- Gate both-ways: `assert-ready.sh` against a staged config REFUSES before
  its preflight and passes after a PASS verdict; `rehearsal-*` passes
  unconditionally.
- The launcher integration is covered by `tests/launch_event_posture.rs` /
  `tests/launch_webcast_posture.rs` (green with the gate blocks in place,
  2026-08-04).
- `install-t0-agents.sh --rehearsal` is the honest launchd-fired drill
  (fires in ~2 min; URL-MISSED page delivery + paste→pcm-gate→launch).

## Migration status

New node (2026-08-04, prep-night for the 10-event 2026-08-05 slate). Journal
insertions cover the tonight-critical actors (launch-event, launch-webcast,
arm-on-url, run-arm, event-autorun, redial-supervisor, preflight-event);
remaining actors (slate-stage, variant-study, webcast-autorun,
install-event-timer, run-verdict) get theirs in the week's follow-up. The
event-status reader (`scripts/event-status.sh`) lands next, then this node's
artifacts migrate to Phase D's `var/runs/<event>/<run-id>/` (one path
constant in journal.sh).
