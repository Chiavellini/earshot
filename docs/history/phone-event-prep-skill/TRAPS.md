# Traps — each one cost real time, with the date it cost it

## A stale scheduler job is not the file you edited

Writing a unit file changes nothing until it is reloaded: systemd needs
`daemon-reload` + `enable --now` (`scripts/install-event-timer.sh` does both);
launchd needs `bootout` then `bootstrap`. A config carrying a raised cap plus a
loaded job missing the matching posture flag makes **every** order refuse, and
nothing says so until the call.

**Always re-read the installed job, never the file you wrote.** The 26AUG05 TLN
go-live survived only because it did:

```sh
launchctl print "gui/$(id -u)/$LABEL" | grep -q -- "--fillable" || page "SUSPECT"
```

The systemd analogue is `systemctl --user cat <LABEL>.service` +
`systemctl --user list-timers`, which `scripts/box-event-install.sh` runs and
fails on.

## `--fillable` needs two other flags

`src/main.rs:1062-1067` refuses `--fillable` without **both** `--i-am-live` and
`--allow-mention`. The full mention-FILL posture is
`--i-am-live --allow-mention --fillable`.

Postures, and what each can actually do:

| flags | can it fill? |
|---|---|
| (none) | dry run — orders computed, never sent |
| `--i-am-live` | non-mention markets only, cap ≤1¢ |
| `--i-am-live --allow-mention` | Mentions permitted, still 1¢ **non-crossing — cannot fill by construction** |
| `--i-am-live --allow-mention --fillable` | may cross the ask, up to `MENTION_FILL_CAP_CENTS` |

## Interpreter split — LAPTOP ONLY

Measured on the CDMX laptop 2026-08-05: homebrew `python3` 3.14.6 has `tomllib`
but a **broken `plistlib`** (pyexpat cannot resolve against the system libexpat);
`/usr/bin/python3` 3.9.6 is the exact inverse and predates `tomllib` entirely
(added in 3.11). So on the laptop: **TOML reads use `python3`, plist writes use
`/usr/bin/python3`.**

This is not cosmetic. `registry-admit.py` hit a `tomllib` ImportError under
launchd's minimal PATH during the TLN probe, so `bridge-probe.sh --promote`
silently did not write the verdict — it had to be recorded by hand, and the
failure was invisible until the artifact was inspected.

**The box does not have this problem**: one interpreter, 3.12.3, with both
modules. `box-ready.sh` verifies it under the systemd user PATH anyway, because
"works in my login shell" is not the environment that runs.

## Scheduler environments lack the interactive PATH

`systemd --user` on the box has no `~/.cargo/bin` (measured). launchd on the
laptop is the same. A fire-time `cargo run` therefore cannot work — always run a
pre-pinned binary. Preflight writes `state/<E>/earshot-pinned` + `pinned.info`,
and every launcher auto-selects it.

## Never pipe a gate through `head` or `tail`

The exit status of a pipeline is the **last** command's. Demonstrated live while
building this skill:

```
$ scripts/doc-lint.sh 2>&1 | head -40; echo "EXIT=$?"
doc-lint: FAILED (6 error(s))
EXIT=0
```

Six errors, reported green. Run gates unpiped, or capture with
`cmd > file; rc=$?`, or set `pipefail`.

## `ORT_DYLIB_PATH` must be set, and must not be a dSYM

`streaming_ctc` dlopens onnxruntime. Unset, wrong-platform, or pointed at a
`.dSYM` debug bundle all **hang** rather than error — the 2026-07-29 phone-lane
loss and the 26AUG03 hang. The launchers resolve it from `vendor/` per platform
(`*.dylib` on Darwin, `*.so*` elsewhere, always `-not -path '*dSYM*'`) and refuse
if they cannot. `cargo test` also SIGABRTs in `sherpa_kws` without it.

## Fail-closed is WRONG for a phone-only event

A gate that refuses protects you when there is a fallback. With no webcast leg,
refusing to arm is **zero coverage** — the worst outcome, not the safe one.

Where the evidence supports it, arm anyway under an explicit, **paging** override
that records its reason:

```
EARSHOT_GREEN_OVERRIDE="<one sentence: why this is defensible>"
```

Make the inversion deliberate and documented. Prefer the designed path first: a
venue whose bridge only exists in its event window should carry
`reservation_window = true` in `configs/dial-ins.toml`, which turns preflight's
untested FAIL into a WARN naming the window — no override at all.

Never use the override to paper over a gate telling you something true: a
`dropped` bridge, a dead keyword, a stale binary.

## An unprovisioned bridge ANSWERS, then hangs up

A too-early dial looks **successful** to Twilio and then dies. TLN 26AUG05:
*"Please enter your dial-in pin and press pound when finished. The conference is
not yet available. Please reconnect closer to the scheduled start time."* — cut
at 13.1 s.

What carries across the provisioning moment is **dial attempts plus mid-call
reconnects**: `--dial-attempts 5` (default 3), `twilio_stream.max_reconnects = 2`,
and `--stream-confirm-secs 150` (default 90) so a legitimate redial is not
declared a deaf call. `event-autorun.sh` adds its own loop of up to 25 attempts
cycling three DTMF timing variants.

## An off-window probe classifies NOTHING

For a reservation-window venue, a no-answer is not evidence of anything. AAPL is
the type specimen: rang 55 s, `status=no-answer`, zero media at 03:40 UTC, with
numbers and code byte-identical in Wayback since 2024 — the bridge was not dead,
it was not yet provisioned. Probe at T-90..T-30;
`scripts/admission-window-probe.sh` refuses outside that window (exit 4).

## Counting report entries is not counting orders

The 1¢ mention-probe posture short-circuits before `send_order()`, so
`report.json` accumulates records that never reached the wire. That is how "23
orders placed" was reported when the true number was zero.

**Truth is `orders[].send_ts != null`** (set immediately before the request hits
the wire) plus the per-event ledger delta. The `$1` `probe_ledger.json` lives in
`--state-dir`, so N concurrent events carry N × the nominal cap.

## Ports must agree in three places

The listener's `bind` in the config, `BIND_PORT` in the event env, and the
`/e/<port>` path in `EARSHOT_STREAM_URL` — plus the route must exist in the box's
`/etc/caddy/Caddyfile`. Any disagreement and no media arrives, silently.

Until 2026-08-05 `event-autorun.sh` **declared** `BIND_PORT` in every env and
then ignored it, hardcoding `--bind-port 8080`. Fixed; it now honors
`${BIND_PORT:-8080}` and uses that port for its stale-port cleanup too (a
hardcoded 8080 kill from event B would have taken down event A's listener).

## `configs/*.env` are tracked; `state/` is not

18 tracked `.env` files under `configs/`. Digits live only in
`state/hunt/numbers.env` (0600) and are read at **source time** by a command
substitution in the tracked env, so preflight's dial-in gate passes without a PIN
entering git.

Two sharp edges:

- An unresolved lookup yields an **empty** `TO_NUMBER`, and preflight's gate only
  FAILs on a literal `TBD` — so empty would sail through as "resolved" with no
  number. Always `TO_NUMBER="${TO_NUMBER:-TBD}"`.
- `doc-lint.py::validate_dial_in_digits` scans `configs/dial-ins.toml` for
  6–15-digit runs outside a small set of digit-safe keys. `admission_detail` and
  `admission_note` are **not** digit-safe.

## `*` is never a DTMF character

Charset is `[0-9#w]` (`w` = 0.5 s wait). Star codes raise a hand or unmute;
the posture is listen-only, always. `twilio-dial.sh` (exit 3) and
`event-autorun.sh` both reject it before dialing.

## `fanout` needs two sources

`[arbiter] mode = "fanout"` with fewer than 2 sources fails config validation
(`src/config.rs:704`). A phone-only arm has exactly one, so it must **omit**
`[arbiter]` entirely — the default single-source arbiter is correct.

## The two hashes have different scopes on purpose

- `config_sha256` — the whole file, byte for byte. Edit a comment and the
  preflight verdict is void; re-run preflight.
- `keywords_sha256` — ticker + phrase + variants only, **not** caps or
  thresholds. Retuning a cap does not invalidate a TTS study, which is what lets
  a derived phone config reuse its webcast twin's readiness artifact and
  dead-word ack.

## Rehearse with the production mechanism, or it proves nothing

An unattended path counts as validated only when it was **launchd/systemd-fired**
(not a manual run), killed with the **real signal** (SIGTERM — what `kill`,
systemd and traps send — not the STOP file), under the scheduler's minimal env.
Equivalent-looking manual drills validated the wrong paths before the 2026-07-15
MS run.
