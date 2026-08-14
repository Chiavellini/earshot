# Work order S — SCHEDULE: the typed scheduler fires webcasts on the box

worker: box-schedule-lane · branch: `webcast-lane` · as_of 2026-08-13
Master: `docs/work/BOX-WEBCAST.md` (seams S-B, S-D, S-E, S-F bind this lane).
Ledger ids reference `docs/SYSTEM-STATE.md` §3.

## Mission

Today the scheduler treats lane as a host proxy: `schedule-nightly.sh`
hard-routes webcast→launchd→laptop and phone→systemd→box (`:322-359`), and
both installers refuse the other lane (`install-event-timer.sh:86`,
`install-t0-agents.sh:146`). This lane makes dispatch **host-directed** —
route on the plan's `execution_host_id` — and gives the box a webcast
install path with the same loaded-readback proof discipline the laptop
lane earned the hard way (L3, L6, L7). Resolver transport is lane R's job;
box provisioning and preflight are lane H's.

## What already exists — do not rebuild

- `install-event-timer.sh` writes correct systemd user units for phone
  (T-25, `event-autorun.sh`, `TimeoutStopSec=150`, `KillMode=mixed`,
  Linger check at `:164-168`). The unit mechanics are proven; only the
  lane assumptions are wrong for webcast.
- `box-event-install.sh:196-251` — staged rsync →
  `run-host-boundary.py reconcile --expected-host` → verify. This block is
  lane-agnostic (`run-host-boundary.py` has zero lane branches). Reuse it
  verbatim; do not reinvent dispatch.
- `schedule-proof.py` already admits `systemd-user` (`:657`) with
  TimeoutStopSec/KillMode rules (`:684-696`); `expected_program` and the
  75-min webcast lead (`:563-576`) are lane-correct already.
- `install-t0-agents.sh` (laptop) is DONE — PATH pin, Darwin-25 readback,
  `--selftest-readback` (L6/L7 closed 2026-08-13). Touch it only for the
  optional S6 resolve-push mode.
- `webcast-autorun.sh` is a thin `exec arm-on-url.sh` facade — schedule
  `arm-on-url.sh` directly, as the laptop plist does (`install-t0-agents.sh:192-193`).

## Work items (priority order)

1. **S1 — lane-aware `install-event-timer.sh`.** Accept
   `lane ∈ {phone, webcast}` (drop the refusal at `:86-87`): webcast ⇒
   fire = start − 75 min (not the hardcoded T-25 at `:112`), `ExecStart` =
   `arm-on-url.sh --run-dir <R> --i-am-live --fillable`, unit env gains
   `EARSHOT_ACCOUNT_ENV` (mirroring `install-t0-agents.sh:255-272`; today
   the unit env at `:132-144` lacks it). Keep `TimeoutStopSec=150` /
   `KillMode=mixed` / Linger. Add `--fire-at-utc` recovery-override parity
   with the launchd installer (`install-t0-agents.sh:24`). Verify the
   LOADED unit (`systemctl --user show`), never the file written.
2. **S2 — `scripts/box-webcast-install.sh`.** New installer reusing the
   `box-event-install.sh:196-251` staging/reconcile/verify block: requires
   `--run-dir`, a canonical schema-2 AccessPlan, and a plan whose
   `execution_host_id` matches the box; refuses `static-public` and
   accepts `preresolved` (seam S-E); checks the box pin exists at
   `state/<E>/earshot-pinned` (lane H builds it — verify, don't build);
   no digit store, no ports, no Twilio gates. Installs via S1's timer and
   re-reads the loaded unit.
3. **S3 — scheduler-aware `schedule-proof.py`.** `expected_environment`
   (`:579-596`) pins the Homebrew `PATH` for every webcast plan; make it
   scheduler-aware (seam S-D): launchd ⇒ PATH pin; systemd-user ⇒ no PATH
   override (the box default is healthy — the comment at `:591-596`
   already says so). `EARSHOT_ACCOUNT_ENV` required for webcast on both.
   Extend the offline fixtures with a systemd webcast readback.
4. **S4 — host-directed dispatch in `schedule-nightly.sh`.** Replace the
   lane fork (`:322-366`): webcast plans route by `execution_host_id` —
   equal to `EARSHOT_LOCAL_EXECUTION_HOST` ⇒ `install-t0-agents.sh`
   (unchanged); equal to `EARSHOT_BOX_EXECUTION_HOST` (default
   `earshot-useast1`, seam S-B) ⇒ `box-webcast-install.sh`; anything else
   ⇒ REFUSED loudly. Phone routing unchanged. Update the offline stub
   assertions (`:154-160`) to prove all three routes.
5. **S5 — laptop resolve-push job (stretch, only after S1–S4 are green).**
   For `preresolved` box events, `install-t0-agents.sh` gains a mode that
   schedules `push-url-file.sh` (lane R's script — call, never edit) on
   the laptop at the same T-75, so the push is not a human's memory. If
   time runs out, document the manual push step in the contract doc and
   leave S5 on the record as open.

## Owned nodes (claim under these; one open claim per node)

- `control.operate` — extend the existing open claim
  (`schedule-nightly.sh`, `install-t0-agents.sh` if S5,
  `tests/typed_scheduler_boundary.sh`, `docs/nodes/control-operate.md`).
- `control.supervise` — extend the existing open claim
  (`install-event-timer.sh`, `box-event-install.sh` if touched, new
  `box-webcast-install.sh`, `schedule-proof.py`,
  `docs/nodes/control-supervise.md`).

New files go into `architecture/nodes.toml` owns AND the node's open claim
(seam S-F). If `opsctl schedule` user-facing text changes, check
`architecture/operations.toml` + `python3 tests/ops_surface.py` before
assuming green.

## Forbidden zones

- `scripts/access-plan.py`, `scripts/arm-on-url.sh`, `urlfile-wait.sh`,
  `push-url-file.sh` internals → lane R (S5 calls push-url-file; never
  edits it).
- `scripts/box-ready.sh`, `scripts/preflight-event.sh`,
  `scripts/deploy-box.sh`, `scripts/execution-profile.sh` → lane H.
- `scripts/launch-webcast.sh`, `scripts/event-autorun.sh` phone behavior,
  `src/**`, `src/risk.rs` money constants, `.claude/skills/**` (the
  `SKILL.md:250` env-var note update is a close-out boundary request).

## Rules

- Claim per `AGENTS.md` in `architecture/claims.toml` before touching a
  node; contract doc must match code before a claim closes.
- `docs/STATUS.md` / `docs/work/ACTIVE.md`: append your own section only.
- Money policy (§4) untouched. No live scheduling against a real event
  during development — offline selftests + drill configs only; the
  integration rehearsal (master §8) is the operator session's.
- Verify the LOADED descriptor, never the file you wrote (L3/L6 — the
  standing lesson of this repo's scheduler history).

## Acceptance

- `schedule-nightly.sh` offline selftest proves the three-way route
  (local webcast ⇒ t0-agents argv, box webcast ⇒ box-webcast-install
  argv, phone ⇒ unchanged) and the loud refusal for an unknown host.
- A box-compiled webcast RunPlan installs a systemd timer + unit whose
  LOADED readback passes `schedule-proof.py` end-to-end, including the
  scheduler-aware env rules and `--fire-at-utc` parity.
- Laptop webcast scheduling is byte-identical for local plans
  (regression: existing `tests/typed_scheduler_boundary.sh` and
  `tests/schedule_proof.py` stay green untouched or minimally extended).
- Verification: `tests/typed_scheduler_boundary.sh`,
  `python3 tests/schedule_proof.py`, `python3 tests/opsctl.py`,
  `python3 tests/ops_surface.py`, `bash -n` on every touched script,
  `scripts/doc-lint.sh`, plus every new test red-then-green.
