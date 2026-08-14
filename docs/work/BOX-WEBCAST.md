# Box-webcast migration — the full workflow

as_of 2026-08-13 · branch `webcast-lane` · master for three parallel lanes:
`docs/work/BOX-RESOLVE.md` (R), `docs/work/BOX-HOST.md` (H),
`docs/work/BOX-SCHEDULE.md` (S). Ledger ids cite `docs/SYSTEM-STATE.md` §3.

## 1. Mission

Webcast events currently execute only on the CDMX laptop (launchd). Move
webcast execution to the us-east-1 box the same way the phone lane already
runs there — because the box measurably wins: media CDN TTFB ~51–55 ms and
0% loss vs the laptop's lossy long-haul path (`box-ready.sh:20-23`,
`docs/LATENCY-PROOF.md:2708-2712`), 16 idle cores vs 8 contended ones (C5:
FIG lost 11.3% of frames at laptop load 150). AMAT 26AUG13 ran on the laptop
because no box path existed; this program builds that path. The laptop lane
stays intact and remains the default until the box rehearsal passes and the
operator rules on the first live box event.

## 2. What is already true — do not rebuild

- **`scripts/launch-webcast.sh` is Linux-clean.** Zero macOS-isms; the ORT
  branch picks `libonnxruntime*.so*` off `uname` (`:177-185`); own-pgroup
  helper prefers `setsid` — "setsid where it exists (the box)" (`:591-597`).
  No source change is in scope for it.
- **`scripts/pull-box-state.sh` is lane-agnostic** (E3's cure). It already
  pulls `state/<E>/` and `var/runs/<E>/<R>/` for any lane. No change.
- **`scripts/arm-on-url.sh` re-expands `$HOME/` argv tokens on the executing
  host** (`:102-105`) — a plan bound on the laptop resolves url-files to
  `/home/ubuntu/...` on the box automatically. Its resolver retry loop
  (30 s to `RESOLVER_DEADLINE_EPOCH`, wall exit-5 paging) runs any argv.
- **Curl-class resolvers are box-portable today**: `q4-resolve.sh` (pure
  curl), `choruscall-resolve.py` (stdlib), `cdn-cachebust-fetch.sh`.
- **`scripts/preflight-event.sh` already anticipates the box**: Darwin-only
  gates are guarded, Linux ORT resolution exists (`:405-412`), and a
  non-git checkout degrades to a loud WARN (`:361`), not a refusal.
- **`scripts/schedule-proof.py` already admits `systemd-user` readbacks**
  (`:657`) with the TimeoutStopSec/KillMode rules (`:684-696`).
- **A laptop-minted URL is consumable from the box** — proven 200s from
  us-east on a laptop-session token (`docs/LATENCY-PROOF.md:2735-2740`).
  Token lifetime is unmeasured; that is R-lane work, not an unknown unknown.

## 3. Invariants — cannot be traded away by any lane

1. **E3 stays law.** `deploy-box.sh` excludes `state/` and `var/runs/` both
   directions with `--delete`. No lane relaxes those excludes, ever. Pin and
   evidence transport go around them, never through them.
2. **A run is compiled FOR a host, never moved.** `execution_host_id` is
   inside `plan_sha256` (`eventctl.py:530,558`); changing hosts = a new
   RunPlan attempt from `init` onward. Box events are box events at stage
   time.
3. **Browser resolution stays laptop-side.** The box has no playwright and
   no operator to pass a wall; registration venues keep THE AMAT RULE
   (prep-time profile on the laptop). The box consumes URLs; it does not
   mint browser sessions.
4. **YouTube-class venues stay on the laptop.** The box egress is bot-walled
   at the segment layer — a pushed manifest does not help when media fetches
   are refused (`pcm-gate.sh:277-280` fails closed there by design).
5. **Money policy is untouched** (`docs/SYSTEM-STATE.md` §4, `src/risk.rs`).
   Drills use the probe envelope; never point a recorder at a tradeable
   call.
6. **An unattended path is validated only by the production mechanism**:
   systemd-fired (not a manual run), killed with real SIGTERM, under the
   unit's minimal env (CLAUDE.md; the 2026-07-15 lesson).

## 4. The target workflow (end-to-end, one box webcast event)

1. **PREP (laptop).** Slate → stage as today, but the RunPlan is compiled
   with `execution_host_id = earshot-useast1` (host id via the box account
   env / `execution-profile.sh resolve`). Study + posture stay on the laptop
   (variant-study needs macOS `say`); configs and `.readiness/` are tracked
   files and reach the box via `deploy-box.sh`.
2. **ACCESS (laptop).** `access-plan.py bind` per venue class:
   - curl-class (`q4-api`, `choruscall`): bind as today — the resolver argv
     executes natively on the box.
   - browser-class (`browser-network`, incl. registration walls): bind the
     new `preresolved` kind — box side waits for a fresh
     `$HOME/<slug>-stream.url`; laptop side resolves (with profile where
     needed) and pushes it (`push-url-file.sh`).
   - `youtube-live`: laptop-hosted event (invariant 4).
3. **DEPLOY + HOST PROOF.** `deploy-box.sh`; `box-ready.sh --config <cfg>`
   must reach a meaningful GO for a webcast config (tools, ORT, model,
   disk, NTP); `fetch-models.sh` on the box if the engine's model dir is
   missing.
4. **PREFLIGHT (on the box).** `preflight-event.sh` runs on the box over
   SSH from a clean deploy; the pin lands at box `state/<E>/earshot-pinned`
   where `arm-on-url.sh:133-138` demands it. Green snapshot lives with it.
5. **SCHEDULE (laptop).** `opsctl schedule` routes by the plan's
   `execution_host_id`: local → `install-t0-agents.sh` (unchanged); box →
   `box-webcast-install.sh` → lane-aware `install-event-timer.sh` (T-75,
   `ExecStart` = `arm-on-url.sh --run-dir … --i-am-live --fillable`,
   `EARSHOT_ACCOUNT_ENV` in the unit env). `schedule-proof.py` verifies the
   LOADED unit, never the file written.
6. **FIRE (box).** The systemd timer fires arm-on-url; the resolver leg is
   native (curl-class) or `urlfile-wait.sh` (preresolved). For preresolved
   events the laptop pushes the url-file inside the window (scheduled
   resolve-push job, or operator during the bring-up phase). pcm-gate PCM
   proof → `capture_ready` → `launch-webcast.sh` arms. Paging works from
   the box via `notify.sh` mail (`~/.kalshi/mail_env` is provisioned
   there); the osascript/say channels are skipped on Linux — accepted.
7. **AFTER.** `pull-box-state.sh --date <D>` before ANY grader (E3), then
   `missed-arm-check.py`, run-audit, journal.

## 5. The three lanes (disjoint node ownership)

| Lane | File | Nodes | Builds |
|---|---|---|---|
| R — RESOLVE | `docs/work/BOX-RESOLVE.md` | `control.acquire`, `control.launch` | `preresolved` AccessPlan kind, `urlfile-wait.sh`, `push-url-file.sh`, vantage decoupling, token-lifetime measurement |
| H — HOST | `docs/work/BOX-HOST.md` | `platform.deploy`, `control.preflight` | webcast-capable `box-ready.sh`, preflight-on-box path + pin provenance, host identity/fingerprint provisioning, models/ORT proof |
| S — SCHEDULE | `docs/work/BOX-SCHEDULE.md` | `control.operate`, `control.supervise` | lane-aware `install-event-timer.sh`, `box-webcast-install.sh`, scheduler-aware `schedule-proof.py`, host-directed `schedule-nightly.sh` dispatch |

## 6. Cross-lane seams — fixed here; lanes do not renegotiate

- **S-A (url-file transport, lane R owns both ends):** path is
  `$HOME/<slug>-stream.url` on the executing host; pushed over SSH with the
  URL on **stdin** (never argv — `ps`-visible, and these URLs carry tokens),
  written atomically (tmp + `mv`). Freshness: a url-file from a prior
  attempt must be refused; `urlfile-wait.sh` accepts only files whose mtime
  falls inside the current attempt's fire window, tunable via
  `EARSHOT_URLFILE_FRESH_SECS`.
- **S-B (routing env, lane S owns):** `EARSHOT_LOCAL_EXECUTION_HOST` keeps
  its meaning (the laptop id); the box side is
  `EARSHOT_BOX_EXECUTION_HOST`, default `earshot-useast1`. Routing compares
  the plan's `execution_host_id` against these — lane is no longer a host
  proxy.
- **S-C (pin transport = preflight on the box, lane H owns):** decided —
  option (a). No narrow `state/` push, no exclude changes. `deploy-box.sh`
  may additionally stamp a `DEPLOYED-FROM` provenance file (commit + dirty
  count) so the box pin's `pinned.info` can cite real provenance instead of
  `NOT-A-GIT-CHECKOUT`.
- **S-D (expected env is scheduler-aware, lane S owns):** the Homebrew
  `PATH` pin applies to launchd readbacks only; systemd units keep the
  healthy default. `EARSHOT_ACCOUNT_ENV` is required for webcast units on
  both schedulers.
- **S-E (`preresolved` is first-class):** installers must accept it —
  `install-t0-agents.sh:151-154` refuses only `static-public` today; keep it
  that way, and `box-webcast-install.sh` mirrors. The kind never bypasses
  fire-time PCM proof; pcm-gate + `capture-ready` run unchanged after the
  resolver leg.
- **S-F (new-file governance):** `urlfile-wait.sh` + `push-url-file.sh` →
  `control.acquire` owns; `box-webcast-install.sh` → `control.supervise`
  owns. Each lane adds its new paths to `architecture/nodes.toml` owns and
  to the **existing** open claim of that node (one open claim per node —
  never a second claim).

## 7. Not in scope / operator rulings

- The **first LIVE box webcast event** is an operator ruling taken after the
  rehearsal in §8, not a lane decision.
- Re-labeling historical Δrace data for the new us-east capture vantage:
  lanes must not pool vantages (R4); any retro-analysis is separate work.
- `daily_loss_cap_cents`, ledger scope, fill caps: §4, operator only.
- The laptop webcast lane is not deprecated by this program; it remains the
  path for YouTube-class venues and the fallback for everything.

## 8. Program acceptance — the integration rehearsal

Run after all three lanes close, by the integrating operator session:

1. A **drill webcast config** (observe-only posture, probe envelope, no
   open tradeable board) staged with `execution_host_id = earshot-useast1`.
2. Full chain, production mechanism only: init → bind (`preresolved`) →
   deploy → `box-ready` GO → preflight on the box → `opsctl schedule`
   routes to the box → systemd timer LOADED readback proven → fires at a
   real future T-75 → laptop pushes the url-file → PCM proof →
   `launch-webcast` arms → **SIGTERM kill drill mid-run** → terminal
   receipts → `pull-box-state.sh` → graders see the run (not "0 orders,
   nothing local").
3. Verification: each lane's gates, plus `scripts/doc-lint.sh`,
   `python3 tests/ops_surface.py`, full `cargo test` (with
   `ORT_DYLIB_PATH`), and the three offline selftests
   (`schedule-nightly` stub proof, `schedule-proof --check`,
   `access_plan.py` tests).
