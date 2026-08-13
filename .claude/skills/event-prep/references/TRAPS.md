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

## `posture-from-study.py` is invoked by nothing automatic (2026-08-12)

`opsctl prepare-day` stages, studies, preflights, and schedules — it never sets
posture. A board whose study came back green still ships at whatever caps the
config already had (usually 1¢ observe) unless you run posture explicitly. And
posture edits change config bytes ⇒ void the preflight verdict ⇒ re-preflight.

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
