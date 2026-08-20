# HANDOFF — 26AUG20 event prep (SUPERSEDES the 19:40Z version)

Written 2026-08-19 ~21:00Z by the reliability/event-prep agent. Repo HEAD:
`9b6796f`. Events tomorrow: **FUTU 11:30Z (box-primary), WMT 12:00Z
(laptop-primary), DE 14:00Z (box-primary)** — ~14.5 h to FUTU as of this writing.

The mandate from the operator was blunt: **the system MUST capture reliably at
the absolute minimum.** The 26AUG19 double miss was a live-session single point
of failure. This session fixed the capture-path bugs and cleared the prep
blockers; the remaining chain (posture → preflight → schedule → OS jobs) is
gated on the study, which is still running.

## 0. TL;DR — state right now

- **DONE & committed (`9b6796f`, scripts-only, box redeployed to it clean):**
  the capture-reliability patches (§2), the two 19:40Z blockers cleared,
  `tailwind` widened, box twins re-derived. All self-tests green; doc-lint at
  its 2-error baseline (the pre-existing bakeoff ones).
- **Minting legs PRE-VALIDATED for tomorrow (§3)** — the AMAT-rule check passed
  on all three: FUTU profile is past its registration wall, DE Q4 API 200 on the
  box, WMT youtube id is a scheduled-live in ~15 h.
- **Study COMPLETE & validated** (§4, 21:35:58Z, `problems: 0`): all 5 readiness
  artifacts written, NO dead words, weak words → OBSERVE. Go straight to posture.
- **Remaining chain: posture → re-init/re-bind → preflight → box dispatch →
  schedule → OS jobs + rehearsal (§5).** All gated on a clean study.
- **Two OPERATOR asks that only a human can do (§7): AC power + a page receiver.**

## 1. The 26AUG19 failure (context, unchanged)
Session died 05:38Z on a `[cyber]` safeguards flag for 12 h; every monitor was
session-scheduled and fired 5 h late; url push was local-only; TOL armed a
0-frame master; TGT's webinar.net is WebRTC. launchd + 10 mail pages worked —
nobody received them. Full timeline in git history of this file (`d044716`).

## 2. What is committed this session (`9b6796f`)

Capture-path bugs found in the new robustness scripts — none had ever been
launchd-fired, so none had been exercised on a live event:

- **`arm-on-url.sh`** — the PCM gate now RETRIES on a bounded schedule until the
  AccessPlan deadline (`EARSHOT_PCM_RETRY_SECS`, default 15). It was single-shot:
  AS 2026-08-18 it hit a transient IVS 404 20 s after the URL landed and the
  unit EXITED while the call streamed fine. First failure pages once; only the
  deadline pages `NOT ARMED`. Rig case 6 added (flaky ffmpeg → one launch, one
  retry page, no false NOT-ARMED).
- **`browser-resolve.py`** — `MANIFEST-SEEN` prints the FULL url (was `[:180]`);
  a tokenized Notified media playlist is >180 chars, so url-push-job was feeding
  a CUT (corrupt) fallback candidate — the exact TOL media-playlist path.
- **`url-push-job.sh`** — every resolve leg is BOUNDED to `--until-utc` (own
  pgroup, killed at the bound) so a `browser-resolve --wait-secs 6600` leg can
  no longer outlive the window and skip the deadline page. `--check` covers it.
- **`laptop-failover.sh`** — real `--install` (wraps the loaded
  `com.earshot.run.<sha>` plist, readback); watches the box the WHOLE window
  (LATE failover if the box dies after `capture_ready`); immediate takeover on
  box terminal/dead-unit; UNREACHABLE-at-decision counts as failed (capture
  beats the one-trader rule). Stops box unit+timer before arming.
- **`event-watchdog.sh`** — pages when NO leg captures, or a ready leg has since
  died — NOT when the (by-design idle) failover leg is quiet. Fixed the
  `grep -c || echo 0` double-line that made box counts garbage; page body now
  carries box unit state + `arm-on-url-<R>.log` tail.
- **`box-webcast-install.sh` / `schedule-nightly.sh`** — ssh command array
  carries `-n` and the scheduler redirects each installer's stdin, so a box
  dispatch no longer eats the `while read` manifest (memory
  `schedule-nightly-ssh-truncates-manifest`). NOTE: rsync's transport ssh
  deliberately does NOT get `-n` (it needs stdin).

SKILL Phase 8 + `docs/nodes/control-launch.md` updated to match.

## 3. Minting legs — measured 2026-08-19 ~20:34Z (the pre-day proof)

| event | check | result |
|---|---|---|
| FUTU | `browser-resolve` against profile `/Users/bernardodelrio/.earshot/profiles/futu` | **past the wall**: PAGE-LOADED, resolves master `media.main.pro2.mas…/HLSv4.m3u8`, ignores the two holding VODs (ENDLIST), writes the master. pcm-gate FAILs it right now only because pre-call it IS the VOD — the live master mints at call and `--reject-endlist` skips this one. Box `curl` of the media path = 200 (measured 19:00Z). |
| DE | box `curl https://attendees.events.q4inc.com/rest/v1/event/531842209` | 200, `eventStart 2026-08-20T14:00:00Z`, `broadcastUrl ''` (mints at call). Box self-resolves. |
| WMT | `yt-dlp` on `hOdyv3MKhPE` | "This live event will begin in 15 hours" — id valid, scheduled-live; `youtube-resolve.sh` polls `is_live` at call time. |

## 4. Study — COMPLETE & VALIDATED (21:35:58Z, `problems: 0`)

Ran via a load-gated wrapper (waited for load<8 + `say`<3.5 s, then
`opsctl.sh study --date 2026-08-20`). Log: `…/834c0382-…/scratchpad/study-26aug20.log`.
All 5 readiness artifacts written; STUDY-EXIT 0. **No dead words anywhere** — no
0-confirmed rows, no acks written (`configs/.acks/` has no `*26aug20*` file). A
few benign single-variant TTS timeouts (perp/efficiency/excavator) but every
affected word still scored fine. I checked for contention-fabricated dead words:
`headwind` scored 0.44 in BOTH FUTU and DE (reproducible = real, not noise), so
the study is trustworthy despite later load spikes. Ratios = confirmed/clips:

- **FUTU** (12): australia 3.78, expert 3.11, fraud 2.67, agentic 2.33, staking
  2.11, tailwind 1.83, cftc 1.70, tokenized 1.50, perp 1.37 → FILL; **headwind
  0.44, kalshi 0.22, panthertrade 0.56 → OBSERVE 1¢** (below the 1.0 floor).
- **WMT** (14): all ≥1.0 except **upper income 0.89 → OBSERVE**. Strong: sparky
  3.22, prescription 3.56, rollback 3.11, gas 2.76, marketplace 2.74,
  competition 2.44, snap 2.22, brick-and-mortar 2.22; omnichannel exactly 1.00.
- **DE** (16): all ≥1.0 except **headwind 0.44 → OBSERVE**. **tailwind now 66/36
  = 1.83** (the widen fixed the old 0/18 auto-ack). Strong: excavator 4.50,
  manufacturing 3.56, oil 2.62, efficiency 2.38, dividend 2.33, repair 2.33.

→ Study needs NO re-run. Proceed straight to posture (§5.1). The next agent
should still glance at the WMT rows above vs its own read of the readiness JSON,
but I found nothing suspect.

## 5. Remaining chain (all gated on a clean study)

**Active run-dirs BEFORE posture** (they CHANGE after the posture re-init):
FUTU `…-3784fd8e1204`, FUTU-BOX `…-c97cbb28dc83`, WMT `…-74aec93d2d71-r2`,
DE `…-be1a997ee8c0`, DE-BOX `…-bb88c5540ea0`.

### 5.1 Posture (the money step — operator ruling 26AUG19, same as TGT/TOL)
For each LAPTOP cfg (futu, wmt, de): set `[risk].max_order_notional_cents = 1940`
(the TGT/TOL comment form, per-word cap = 20×97¢), then:
```
python3 scripts/posture-from-study.py configs/webcast-<x>-26aug20-arm-wc.toml \
  --compile-with target/release/earshot \
  --model-dir models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18 \
  --size-tiers 3.0:20,2.0:10,1.0:4 --low-band-budget 2
```
Read the printed WORST-CASE notional against `PROBE_LEDGER_CAP_CENTS = 20000`
(src/risk.rs:77, per-event ledger) — record it in the journal, don't just check
it fits. Weak words (ratio<1.0) go OBSERVE 1¢ unless the low-band budget lifts
them. Do NOT posture the box twins directly — re-derive them (§5.2).

### 5.2 Re-init + re-bind (posture changed the config bytes ⇒ E16 drift)
- `record --transition blocked` the 5 current attempts (local authority, never
  dispatched — a clean append).
- Laptop ×3: `python3 scripts/eventctl.py init --config
  configs/webcast-<x>-26aug20-arm-wc.toml --event-env configs/<x>-26aug20-autorun.env`.
- Box twins ×2: `python3 scripts/webcast-box-twin.py --from
  configs/webcast-{futu,de}-26aug20-arm-wc.toml --force --init` (re-derives from
  the POSTURED laptop cfg — posture the laptop FIRST; box identity
  `earshot-useast1` / `sha256:d13943b5…`, readiness cloned).
- Bind each with the SAME argv as the current AccessPlans (read from each old
  `control/access-plan.json` — see §6 for the exact three shapes). Then
  `scripts/opsctl.sh access --date 2026-08-20` must PASS 5/5.
- **Do NOT run `opsctl stage` / `eventctl init-date` after this** — init-date
  would re-init the box twins with the LAPTOP identity.

### 5.3 Preflight + schedule
- `git status` clean → `scripts/opsctl.sh preflight --date 2026-08-20` (pins
  `state/<E>/earshot-pinned` ×5; laptop-preflighting the box twins is the proven
  TGT pattern). Then green snapshots + `green-guardian.sh` T-90 per event.
- Box: `deploy-box.sh` is already at HEAD, but re-run it AFTER posture (the box
  configs changed). Then per FUTU-BOX/DE-BOX:
  `box-webcast-install.sh --run-dir <box run> --dispatch-only` → ssh box
  `scripts/preflight-event.sh --config configs/webcast-<x>-26aug20-box-arm-wc.toml
  --account ~/.earshot/ms.env --run-dir var/runs/<E-BOX>/<R>` →
  `pull-box-state.sh --prelaunch --event <E-BOX>` →
  `box-webcast-install.sh --run-dir <box run>` → read `systemctl --user
  list-timers` on the box (schedule → pull → close → dispatch, never skip).
- Laptop: `export EARSHOT_LOCAL_EXECUTION_HOST=bernardos-macbook-air-5
  EARSHOT_BOX_EXECUTION_HOST=earshot-useast1; scripts/opsctl.sh schedule
  --date 2026-08-20`; verify every LOADED unit fires in the FUTURE
  (`launchctl print`), never trust `scheduled=N`.
- `scripts/assert-ready.sh configs/<each>.toml`.

### 5.4 OS jobs (THE fix — nothing may depend on a live session), all read back
- FUTU push (ONLY preresolved event): `scripts/url-push-job.sh --install --slug
  futu --at-utc 11:15 --until-utc 11:44 --run-dir <FUTU-BOX run> --host
  ubuntu@3.212.174.94 --run-dir <FUTU-laptop run> --host local` (11:15: the
  browser opens past the wall and is already watching when the live master mints
  ~11:30; the bounded leg guarantees the deadline page). DE self-resolves on the
  box — NO push job (its resolver is q4-api, not preresolved; url-push-job would
  refuse). WMT is the laptop primary — NO push.
- Failover (AFTER schedule, wrap the loaded laptop arm): `scripts/laptop-failover.sh
  --install --run-dir <FUTU-laptop run> --box-event
  KXEARNINGSMENTIONFUTU-26AUG20-BOX-WEBCAST --box-run <R_BOX> --start-utc 11:30
  --grace-min 4` and the same for DE (`--start-utc 14:00 --grace-min 3`). WMT
  needs none. **Never re-run `opsctl schedule` / `install-t0-agents
  --verify-only` after wrapping** (the wrapper reads as an argv mismatch).
- Watchdogs (6): `scripts/event-watchdog.sh --install --slug <x> --at-utc
  <start+5>` and `<start+35>`, `--leg <E-BOX>:<R_BOX>:ubuntu@3.212.174.94 --leg
  <E>:<R>:local` for FUTU/DE, one `local` leg for WMT.
- **Rehearsal under the production mechanism** (CLAUDE.md rule): install a
  `url-push-job.sh --install --slug rehearsal-futu --at-utc <now+3m> --until-utc
  <now+7m>` with the FUTU run-dirs; confirm from the launchd log that it fired,
  ran playwright+profile, rejected the holding VOD, tried the box ssh, and paged
  at the bound — then `launchctl bootout` it. Also dry-run `event-watchdog.sh
  --run …` and eyeball `laptop-failover.sh` decision logic against live state.

## 6. Exact bind shapes (read from the current AccessPlans; keep identical)
- **FUTU** (both legs, same url-file basename `$HOME/futu-stream.url`, deadline
  `2026-08-20T11:45:00Z`): resolver `preresolved` → `urlfile-wait.sh`;
  preresolve `browser-resolve.py --slug futu --url
  https://edge.media-server.com/mmc/p/ex2zp2eh/ --profile
  /Users/bernardodelrio/.earshot/profiles/futu --out $HOME/futu-stream.url
  --click-play --linger-secs 20 --reject-endlist --wait-secs 6600`. Vantage
  `us-east1/futu` (box) | `laptop/futu` (laptop). Keep the profile path ABSOLUTE
  and `$HOME/futu-stream.url` literal (bind-access minted-file rule).
- **WMT** (`youtube-live`, deadline `12:15Z`): argv POSITIONAL
  `youtube-resolve.sh wmt https://www.youtube.com/watch?v=hOdyv3MKhPE 1787228100`
  (epoch, NOT HH:MM). Vantage `laptop/wmt`.
- **DE** (`q4-api`, deadline `14:15Z`): argv FLAGS `q4-resolve.sh --event-id
  531842209 --slug de --deadline-utc 14:15`. Vantage `us-east1/de` | `laptop/de`.

## 7. OPERATOR ASKS (only a human can do these — capture depends on them)
1. **Plug the laptop into AC, lid open.** It is on BATTERY (75%, ~2h47m left at
   20:00Z). `caffeinate` was deliberately NOT started — on battery it would kill
   the laptop before 11:30Z. Preflight's sleep gate will FAIL until AC + a held
   `caffeinate -s` (or a `pmset schedule wake` before 11:00Z). WMT (laptop
   primary) simply cannot fire on a dead laptop.
2. **Name a page receiver.** `~/.kalshi/alert_env` is ABSENT, so every `critical`
   page dies at `alert-phone.sh:31`. Mail delivers to `MAIL_TO` in
   `~/.kalshi/mail_env` but to nobody watching. Either a human on that inbox/phone
   or an automated responder — the 26AUG19 lesson (10 pages, 0 readers).
3. **Other agents' laptop load 11:00–13:00Z endangers WMT** (the only laptop
   primary that day). FUTU/DE capture on the box and are insulated.

## 8. Gotchas that already bit (don't repeat)
- Posture edits change config bytes ⇒ preflight void ⇒ re-init ⇒ new run-dir ⇒
  re-bind ⇒ re-schedule. Do posture BEFORE preflight, once.
- `eventctl record --transition blocked` needs `--outcome ok` (the default);
  `--outcome skipped` is invisible to the reducer (that's why WMT r1 lingered).
- Preflight re-run REBUILDS `earshot-pinned` from the CURRENT checkout — check
  `git status` clean first; don't clobber a green pin.
- Box repo is `/home/ubuntu/ear` (rsync deploy, NOT a git repo); ssh needs
  `-i ~/.ssh/earshot-useast1.pem`; `DEPLOYED-FROM` should read `9b6796f…` (or
  the new HEAD after §5.3's re-deploy).

## 9. Background procs
- The gated study COMPLETED (21:35:58Z) and exited 0 — nothing left running.
- Laptop load was 212 at 21:36Z (other agents). Preflight's `cargo build` will
  be slow under that; posture's `--compile-with` is CPU-light. Not a blocker,
  but the actual event runs capture on the BOX for FUTU/DE — the laptop only
  matters for WMT and for scheduling/installs.
