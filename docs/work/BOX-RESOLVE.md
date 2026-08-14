# Work order R — RESOLVE: the URL reaches the box

worker: box-resolve-lane · branch: `webcast-lane` · as_of 2026-08-13
Master: `docs/work/BOX-WEBCAST.md` (seams S-A, S-E, S-F bind this lane).
Ledger ids reference `docs/SYSTEM-STATE.md` §3.

## Mission

Make every webcast venue class consumable from the us-east-1 box without
putting a browser there. Curl-class resolvers already run anywhere; this
lane builds the **resolver split** for the browser class: the laptop
resolves (with a registration profile where THE AMAT RULE demands one) and
pushes a fresh url-file; the box waits for it and arms. Scheduling, box
provisioning, and preflight are explicitly NOT this lane's job — lanes S
and H own those.

## What already exists — do not rebuild

- `arm-on-url.sh:102-105` re-expands `$HOME/` argv tokens on the executing
  host, and its retry loop (30 s → `RESOLVER_DEADLINE_EPOCH`, exit-5 wall
  paging) runs ANY resolver argv. Validate against it; do not fork it.
- `access-plan.py:47-54` `RESOLVER_KINDS` — `external-command` already has
  `None` for required-script; the schema tolerates a new kind cleanly.
- `q4-resolve.sh` (pure curl, atomic `mv` publish at `:62`),
  `choruscall-resolve.py` (stdlib), `cdn-cachebust-fetch.sh` — box-portable
  today, zero work.
- `browser-resolve.py` wall detection (exit 5), heartbeat, `--profile`
  persistent contexts — landed 2026-08-13 (C9 closed). The laptop resolve
  side of the split is DONE; only the push is missing.
- The feasibility proof: a laptop-session token returned HTTP 200 from
  us-east (`docs/LATENCY-PROOF.md:2735-2740`). Token LIFETIME is the open
  measurement.
- `box-event-install.sh:254-265` is the secrets-over-**stdin** precedent the
  push transport must follow.

## Work items (priority order)

1. **R1 — `preresolved` resolver kind.** Add to `RESOLVER_KINDS` in
   `scripts/access-plan.py` a kind whose bound argv is the new
   `scripts/urlfile-wait.sh <url-file> [--fresh-secs N]`: block until the
   url-file exists AND is fresh (seam S-A: mtime inside the current
   attempt's fire window; stale files from a prior attempt are refused),
   then exit 0; honor `RESOLVER_DEADLINE_EPOCH` and exit 3 past it. It must
   pass through `arm-on-url.sh`'s existing loop unmodified. Validation
   rules: the kind requires a `$HOME/`-portable url-file reference, is
   non-secret like every other kind, and never weakens the PCM-proof path
   (seam S-E).
2. **R2 — `scripts/push-url-file.sh` (laptop side).** Resolve locally
   (execute the laptop resolver argv recorded in the plan, or accept
   `--from-file`), then push to the box atomically: URL bytes over SSH
   **stdin**, remote tmp + `mv`, mode 0644, journal the push. Never the URL
   in argv (tokens are `ps`-visible). `--check` offline selftest with a
   stubbed ssh.
3. **R3 — token-lifetime measurement.** On the next real browser-class
   venue (or a replayable capture of one), measure how long a
   laptop-minted URL keeps returning media from a second vantage; record
   the number and method in the `control.acquire` contract doc and cite it
   as the default `EARSHOT_URLFILE_FRESH_SECS` rationale. If it is not
   measurable before this lane closes, say `not_resolvable` on the record —
   do not guess.
4. **R4 — vantage decoupling.** `access-plan.py:318-321` silently defaults
   `--vantage` to `execution_host_id`. Make the vantage an explicit,
   deliberate label for box-hosted plans (us-east capture must never
   silently pool with CDMX Δrace history — `venue-probe.sh:125`). Keep
   laptop plans byte-compatible. Pooling guards inside
   `system-analysis.py` belong to `evidence.measure` — file a boundary
   request, do not edit it.
5. **R5 — verify, and only then touch, `arm-on-url.sh`.** Expectation: no
   change needed. Prove it with a rig test (preresolved plan + stubbed
   `urlfile-wait.sh`) before editing anything under `control.launch`.

## Owned nodes (claim under these; one open claim per node)

- `control.acquire` — extend the existing open claim
  (`access-plan.py`, `browser-resolve.py`, new `urlfile-wait.sh`,
  `push-url-file.sh`, `docs/nodes/control-acquire.md`).
- `control.launch` — extend the existing open claim ONLY if R5 finds a real
  gap (`arm-on-url.sh`, `docs/nodes/control-launch.md`).

New files go into `architecture/nodes.toml` owns for their node AND into
that node's open claim paths (seam S-F). Never open a second claim.

## Forbidden zones

- `scripts/schedule-nightly.sh`, `scripts/install-*.sh`,
  `scripts/box-event-install.sh`, `scripts/schedule-proof.py` → lane S.
- `scripts/box-ready.sh`, `scripts/preflight-event.sh`,
  `scripts/deploy-box.sh`, `scripts/execution-profile.sh` → lane H.
- `scripts/launch-webcast.sh` internals (call its rig, don't edit it),
  `src/**`, `src/risk.rs` money constants, `.claude/skills/**` (propose
  skill text in your ACTIVE.md close-out).
- `scripts/system-analysis.py` → boundary request to `evidence.measure`.

## Rules

- Claim per `AGENTS.md` in `architecture/claims.toml` before touching a
  node; contract doc must match code before a claim closes.
- `docs/STATUS.md` / `docs/work/ACTIVE.md`: append your own section only.
- Money policy (`docs/SYSTEM-STATE.md` §4) untouched; never point a
  recorder at a tradeable call; probes stay inside the probe envelope.
- Never a URL or credential in argv or git; stdin + env only.

## Acceptance

- A `preresolved` AccessPlan binds, validates, and survives
  `access-plan.py check-day`; `install-t0-agents.sh` still accepts it
  (seam S-E — verify, report if not, lane S fixes).
- In a stubbed rig, `arm-on-url.sh` with a preresolved plan: blocks, then
  arms the moment a fresh url-file appears; refuses a stale one; exits 3
  at deadline with the page fired.
- `push-url-file.sh --check` proves atomic stdin push with zero argv
  leakage.
- Token lifetime measured and recorded, or `not_resolvable` on the record.
- Verification: `python3 tests/access_plan.py`, new urlfile-wait/push
  selftests, `scripts/doc-lint.sh`, `python3 tests/ops_surface.py`, plus
  every new test red-then-green.
