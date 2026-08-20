# Traps — each one cost real time, with the date it cost it

Carried forward from the retired phone-event-prep skill (2026-08-05) and
updated for the opsctl/RunPlan surface (2026-08-12). Obsolete entries dropped;
new ones dated.

## A stale scheduler job is not the file you edited

Writing a unit file changes nothing until it is reloaded: systemd needs
`daemon-reload` + `enable --now`; launchd needs `bootout` then `bootstrap`.
**Always re-read the installed job, never the file you wrote.**

```sh
launchctl print "gui/$(id -u)/$LABEL" | grep -q -- "--fillable" || echo SUSPECT
systemctl --user cat <LABEL>.service; systemctl --user list-timers
```

The typed path now proves this for you: `schedule-nightly.sh` →
`schedule-proof.py` binds the loaded readback to the exact RunPlan. Trust that
proof, and still check the fire time is in the FUTURE (the 08-06 midnight-UTC
loss installed five jobs already in the past).

## `--fillable` needs two other flags

`src/main.rs` refuses `--fillable` without **both** `--i-am-live` and
`--allow-mention`.

| flags | can it fill? |
|---|---|
| (none) | dry run — orders computed, never sent |
| `--i-am-live` | non-mention markets only, cap ≤1¢ |
| `--i-am-live --allow-mention` | Mentions permitted, 1¢ **non-crossing — cannot fill by construction** |
| `--i-am-live --allow-mention --fillable` | may cross the ask up to `MENTION_FILL_CAP_CENTS` |

## The two hashes have different scopes on purpose

- `config_sha256` — the whole file, byte for byte. Edit a comment and the
  preflight verdict is void; re-run preflight.
- `keywords_sha256` — ticker + phrase + variants only, **not** caps or
  thresholds. Retuning a cap keeps a TTS study valid; touching what the
  detector must HEAR voids it. This is what lets a derived phone config reuse
  its webcast twin's readiness artifact, and what `study-nightly.sh` clones on.

## Preflight reruns clobber the pinned binary (2026-08-12)

`state/<E>/earshot-pinned` is keyed on the EVENT ID only. Every preflight rerun
rebuilds from the CURRENT checkout and overwrites the pin in place —
`pinned.info` (commit, dirty_paths, sha256) is the only record of what was
there before. On 26AUG12 a rerun swapped a green 106-dirty pin for a 245-dirty
one. With reliability lanes editing this tree, check `git status` before any
re-preflight.

## Nightly automation commits mid-session (2026-08-12, defect L5)

`slate-daily`, `hunt-daily`, and `stage-nightly` launchd jobs commit
autonomously — they staged YETI-26AUG13 and recreated `slate/changes/` in the
middle of a cleanup. Re-run `git log --oneline -3` before trusting any
minutes-old read, and never `git add -A` in this repo — other actors' work will
be dirty.

## Posture remains explicit, but silence is now blocked (2026-08-12/20)

`opsctl prepare-day` does not invent a money policy, so it never chooses size
tiers or a low-band budget. A board whose study came back green retains the
config's existing caps until `posture-from-study.py` applies an already recorded
operator policy. The 2026-08-20 change is that this omission can no longer look
green: `event-prep-audit.py` emits `NEEDS-POSTURE` when any keyword lacks the
measured marker. Posture edits change config bytes ⇒ new RunPlan ⇒ rebind ⇒
re-study/access/preflight/schedule. Proof from the superseded attempt never
travels to the new bytes.

## Never pipe a gate through `head` or `tail`

The exit status of a pipeline is the **last** command's. Demonstrated live:
`scripts/doc-lint.sh 2>&1 | head -40; echo "EXIT=$?"` printed 6 errors and
`EXIT=0`. Run gates unpiped, or `cmd > file; rc=$?`, or set `pipefail`. (It
happened again on 2026-08-12 — a full cargo failure masked by `| tail`.)

## `ORT_DYLIB_PATH` must be set, and must not be a dSYM

`streaming_ctc` dlopens onnxruntime. Unset, wrong-platform, or a `.dSYM` path
all **hang** rather than error (2026-07-29 phone-lane loss; 26AUG03 hang).
`cargo test` SIGABRTs in `sherpa_kws` without it. Launchers resolve it from
`vendor/` and refuse if they cannot; do the same:
`source ~/.kalshi/earshot_env` first.

## Interpreter split — LAPTOP ONLY

Homebrew `python3` 3.14 has `tomllib` but a broken `plistlib`;
`/usr/bin/python3` 3.9.6 is the inverse. TOML reads use `python3`, plist writes
use `/usr/bin/python3`. The box has one interpreter (3.12.3) with both. This is
why `bridge-probe.sh --promote` once silently failed to write its verdict under
launchd's PATH — **read the registry back, never stdout.**

## Scheduler environments lack the interactive PATH

`systemd --user` on the box and launchd on the laptop have **no
`~/.cargo/bin`**. A fire-time `cargo run` cannot work — the pinned binary is
the only unattended-safe artifact.

## An unprovisioned bridge ANSWERS, then hangs up

A too-early dial looks successful to Twilio, then dies (TLN 26AUG05: cut at
13.1 s with a polite message). What carries across the provisioning moment is
dial attempts + mid-call reconnects (`--dial-attempts`,
`twilio_stream.max_reconnects`, `--stream-confirm-secs`). And an off-window
probe classifies NOTHING (AAPL: 55 s no-answer at 03:40 UTC on a healthy
reservation-window bridge). Probe at T-90..T-30;
`admission-window-probe.sh --run` refuses outside it (exit 4).

## Counting report entries is not counting orders

The 1¢ non-crossing posture short-circuits before `send_order()`; "23 orders
placed" was really 0. Truth is `orders[].send_ts != null` (legacy) or a
`WireAttempted` transition in `orders/order-evidence.jsonl` (canonical).

## Ports must agree in three places

The config's `bind`, the env's `BIND_PORT`, and the `/e/<port>` path in
`EARSHOT_STREAM_URL` — and the route must exist in the box's **installed**
`/etc/caddy/Caddyfile` (repo file = intent, box file = truth). Any
disagreement and no media arrives, silently.

## `configs/*.env` are tracked; `state/` is not

Digits live only in `state/hunt/numbers.env` (0600), read at source time by
command substitution. Two sharp edges: an unresolved lookup yields an **empty**
`TO_NUMBER` which sails through the gate that only FAILs on literal `TBD` —
always `TO_NUMBER="${TO_NUMBER:-TBD}"`; and `admission_detail` /
`admission_note` are NOT digit-safe (doc-lint scans them).

## `*` is never a DTMF character

Charset is `[0-9#w]` (`w` = 0.5 s wait). Star codes raise a hand or unmute;
the posture is listen-only, always.

## `fanout` needs two sources

`[arbiter] mode = "fanout"` with fewer than 2 sources fails config validation.
A phone-only arm has exactly one — omit `[arbiter]` entirely.

## Fail-closed is WRONG for a phone-only event — the one deliberate inversion

With no webcast leg, refusing to arm is zero coverage, the worst outcome.
Prefer the designed path: `reservation_window = true` in `configs/dial-ins.toml`
turns preflight's untested-FAIL into a WARN naming the window. Only where
evidence supports it, and only with the operator stating the reason, arm under
`EARSHOT_GREEN_OVERRIDE="<one sentence why>"` — which pages. Never use it to
paper over a gate telling you something true (a dropped bridge, a dead
keyword, a stale binary).

## Rehearse with the production mechanism, or it proves nothing

Validated = scheduler-fired (not manual), killed with SIGTERM (not the STOP
file), under the scheduler's minimal env. Equivalent-looking manual drills
validated the wrong paths before the 2026-07-15 MS run.

## A registration venue bound `public` loses the whole call (AMAT 2026-08-13)

`edge.media-server.com` MMC players (and any player asking name/email/company)
gate the manifest behind a form. Bound `public`, the headless resolver —
which refuses walls by design — watched that form from T-75 until its watch
expired; zero coverage. Registration venues are `registered-session` with a
persistent browser profile registered AT PREP TIME (SKILL Phase 5, THE AMAT
RULE). `browser-resolve` now exits 5 and pages the moment it sees a wall, and
`arm-on-url` retries until the AccessPlan deadline — but detection-at-fire is
damage control; the binding decision at prep is the fix. AccessPlans are
immutable: a wrong mode or a short `--wait-secs` costs a whole new attempt.

## A "bounded" gate is only as bounded as its slowest child (AMAT 2026-08-13)

The live gate's 3600 s deadline was a top-of-loop guard, and one loop
iteration piped a fetcher that never exits into a command substitution — bash
waits for EVERY pipeline member, so the gate wedged forever at "need one
advance" while the stream ran to its end. Anything unbounded inside a loop
makes the loop's deadline fiction. Both decode legs now run under an
own-process-group watchdog; the regression test stubs the fetcher with the
truth (`while :; do sleep 1; done`), not with `exit 0`.

## The no-cache HEADER does not bust this CDN — only a unique query param does

pcm-gate decoded 541 KB of real audio and still said "VANTAGE IS DEAD":
its playlist poller (`pdt-log.py`) re-fetched the identical URL and CloudFront
served the frozen copy every time. Every playlist fetch now carries `_cb=<ns>`
and the continuity predicate counts distinct TAIL SEGMENTS, never
`#EXT-X-MEDIA-SEQUENCE` (the header this CDN freezes while appending). Rule:
a resolver or gate watching silently is a page-worthy state — never a quiet
one.

## A box-preflighted run needs its receipts pulled before you can schedule it (2026-08-14)

Seam S-C puts webcast preflight ON the box, so `preflight_passed` is minted
into the BOX ledger. But `box-webcast-install.sh` checks `preflight_passed` on
the **laptop's** run dir before it ever opens an SSH connection. Schedule
without pulling first and you get a refusal that reads like a failed preflight
when the preflight actually passed — on the other host.

Pull the pre-launch receipts with the supported door:

```
scripts/pull-box-state.sh --prelaunch --event <EVENT_ID>
```

`--prelaunch` publishes a run that has **not** reached `launched`, which is the
only window where this is safe: before `launched` the run has no `capture/`,
`book/` or `orders/` content, only atomically written control proofs, so there
is no second growing authority to conflict with. It rides the same
direction-agnostic `run-host-boundary.py reconcile` prefix primitive as the
terminal pull, and it deliberately records **no** receipt of its own —
`evidence_pulled` requires a terminal state. Once the prefix is local,
`box-webcast-install.sh` and `schedule-nightly.sh` pass unmodified.

A run that has already reached `launched` is still refused, by design; wait for
terminal and use the ordinary pull.

**Two things will bite you around this pull, both measured 2026-08-16:**

- **Get the run dir onto the box BEFORE preflight** — box preflight refuses
  without the laptop-bound AccessPlan, and `deploy-box.sh` excludes
  `var/runs/`. The supported command is
  `scripts/box-webcast-install.sh --run-dir <run> --dispatch-only`: it
  transfers and publishes the RunPlan prefix and installs nothing. Do **not**
  try to dodge it by compiling the plan on the box instead — `created_utc` is
  inside `plan_sha256`, so the two hosts produce different plans and the
  AccessPlan then refuses to bind.
- **A drill config MUST carry `[event] rehearsal = true`.** Scheduled webcast
  units are `--i-am-live --fillable` unless the pinned config says otherwise.
  With the flag, the installers build a unit with no fill permission, the
  LOADED readback refuses one that has it, and `arm-on-url.sh` refuses the
  flags by hand — so inertness is structural instead of depending on every
  keyword being 1¢. Check with
  `python3 scripts/schedule-proof.py posture --run-dir <run>` (`rehearsal` or
  `live`). Never set it on a real event: it silently withholds every fill.
  Editing the config after the plan is compiled does not flip posture — it
  refuses, because the derivation re-verifies the bytes against
  `plan.config_sha256`.
- **Order of operations is load-bearing: schedule → pull → close → dispatch.**
  A box run's `scheduled` receipt is minted on the BOX. Record anything on the
  laptop copy before pulling it back and the chain forks *permanently* — both
  hosts hold a different receipt at the same index, and no prefix relation can
  ever exist again. Recovery is to close the box copy directly over SSH.
- **Re-preflighting on the box breaks the next pull.** The box rewrites
  `state/<E>/box/detector-smoke.log` and friends, so the puller sees a real
  legacy-state divergence and refuses the whole event. That refusal is correct
  and must stay; the supported way through is
  `scripts/pull-box-state.sh --supersede-legacy --event <E>`, which archives
  the local tree to `state/<E>/box.superseded-<UTC>` and prints which file
  diverged. It never overwrites and never deletes. Do not hand-`mv` it, and
  never make the pull overwrite in place.

**Never close the gap by re-recording the receipts locally**, and never fall
back to a hand `rsync` now that the door exists. A hand-written
`preflight_passed` on the laptop would be a fabricated proof of a build that
happened somewhere else, which is exactly the class the typed ledger exists to
prevent.

## Superseding an attempt does not propagate to the box (2026-08-14)

The corollary of the trap above, in the other direction. Closing a superseded
RunPlan attempt with `eventctl record --transition blocked` on the laptop leaves
the BOX copy still `active`. The next on-box preflight then refuses:

```
REFUSED: eventctl status refused: REFUSED: ambiguous: 2 active runs for <EVENT>
```

which is the right refusal for the wrong reason — the attempt IS closed, just
not where preflight is looking. Push the closed ledger to the box before
re-preflighting; `scripts/deploy-box.sh` excludes `var/runs/` both directions
(E3), so a laptop-side close does not travel on a deploy. This direction has no
one-command door yet — only box→laptop does (`--prelaunch`, above).

## 2026-08-19 — TGT + TOL: two events, two hosts, zero audio (the whole-day miss)

- **The push named one host.** Box-primary legs were staged (correctly) as
  `-BOX-WEBCAST` twins and waited in `urlfile-wait` from T-75; the url pushes
  were published LOCALLY only. Nothing in the chain refused that — the box
  legs logged `URLFILE-WAIT ABSENT` for 75 minutes and the laptop failover then
  stopped them. Fix: `url-push-job.sh` takes every (run-dir, host) pair of the
  event in ONE job; a push job that does not name the box is a missed box event.
- **A minted URL is not a stream.** TOL's resolver rejected the holding VOD
  as designed and pushed a live Notified "metafile" master; the arm launched,
  got 0 frames on both legs, died at +11 min. Fix: `url-push-job.sh` runs
  `pcm-gate.sh` on the minted URL BEFORE publishing and re-resolves on FAIL.
  Never publish a URL that has not produced PCM on the host that will use it.
- **webinar.net is WebRTC.** Registration + Attend works; no HLS/DASH manifest
  ever appears (watched 12:00–12:17Z). The pipe legs cannot ingest it on any
  host. Descope this venue class on the record unless a browser-audio leg exists.
- **Session wakeups are not monitoring.** Four CronCreate observers (10:52,
  11:58, 12:33, 14:05Z) all fired at 17:46Z — they only run while the session
  is idle. The launchd watchdog installed the same night DID run and paged;
  that is the only supervision that counts (`event-watchdog.sh`).
- **"Registered" ≠ "in the room".** webinar.net keeps the registration in
  localStorage but a fresh context bounces `/live` to the registration page
  until "Attend" is clicked — `browser-resolve --click-text Attend`. Proving a
  profile passes the wall is necessary, not sufficient; prove PCM.
- **Re-binding on each config edit.** Posture write → new attempt → rebind →
  preflight → schedule; each cycle is ~8 min and leaves stale launchd jobs for
  superseded attempts LOADED (two fired at the same minute). Always `launchctl
  list | grep com.earshot.run` and bootout the superseded ones; do posture and
  verifier edits BEFORE the first bind.
- **Laptop load gates.** preflight FAILS above 2×cores load; your own
  transcription/bench job (load 30–130 on this laptop) trips it and, worse,
  degrades capture (C5). Nothing heavy on the laptop on event days.

## 2026-08-20 — DE/AAP/ROST: fragments succeeded; the day did not

- **One DNS failure hid two events.** The Aug 18 daily slate sweep died on its
  first resolver failure; its email failed on the same DNS outage. AAP and
  ROST were absent until a human found them. `discover-events.py` now retries
  URLError/timeouts/5xx/429 and publishes atomically. `opsctl prepare-day`
  begins with a fresh sweep; zero events is a failure.
- **The scheduled chain ended at preflight.** Separate nightly stage, study,
  and preflight jobs existed, but no loaded daily arrow invoked scheduling or
  asked whether every event had coverage. The legacy
  `com.earshot.preflight-nightly` label now runs the complete current-UTC-date
  facade, ending in `event-prep-audit.py`.
- **A successful TTS process produced zero audio.** In the sandbox, `say`
  yielded a 78-byte WAV header. The old study incremented its expected count
  without validating samples. Every clip and concat now must be mono 16-kHz
  16-bit PCM with a non-empty payload; otherwise the artifact stays red.
- **Posture superseded the only prepared ROST/AAP attempts.** Their new config
  hashes had active RunPlans but only `plan_compiled`; access and preparation
  remained on old/blocked attempts. The final audit selects active attempts and
  reports the missing lifecycle phase instead of reading any sibling proof.
- **Q4 phone entry was not checked first.** A visible participant dial-in on
  the event page is a real attendee lane and must be qualified before relying
  on browser media. `configuration.dialIn.speaker=true` is only presenter
  bridge metadata; do not claim listener access without the actual participant
  number/PIN.
- **Individual greens did not compose.** DE captured and fired, but several
  eligible detections safely could not cross because the book had no ask or the
  ask exceeded the cap. That is execution truth, not capture failure. The prep
  defect was that no machine joined discovery, access, study, posture,
  scheduling, URL delivery, failover, and watchdogs into one READY verdict.
  `event-prep-audit.py` is now that join.
