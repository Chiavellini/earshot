---
name: phone-event-prep
description: Prepare and run a phone-bridge earnings call end-to-end on the us-east-1 box, unattended, starting from pasted dial-in details. Use when the user pastes a dial-in or registration confirmation (toll-free number, PIN/passcode, call time), or asks to prepare, stage, probe, preflight, schedule, or arm a phone bridge event. PHONE BRIDGES ONLY — it stops rather than build a webcast lane.
---

# Phone-bridge event prep (box-hosted, unattended)

## Purpose

Turn a pasted dial-in confirmation into a scheduled, verified, **box-side** phone
arm that needs the operator only if the bridge is operator-screened.

## The incident this exists to prevent

On 2026-08-05 the Talen (TLN) call was the day's highest-priority event and the
only one with a participant dial-in. It ran on the operator's **laptop in Mexico
City over ngrok** instead of the us-east-1 box, and lost its one contested race
by **534 ms** against Kalshi's public trade tape.

The box was not down — load 0.00, Caddy active, models present, TLS answering.
The blocker, found at ~T-60, was that `~/ear/target/release/earshot` was a day
stale and predated the `MENTION_FILL_CAP_CENTS` 50→97 raise, so
`mention_fill_gate` (`src/kalshi/validator.rs:163`) would have refused the
config's 97¢ cap. A Rust rebuild plus artifact migration did not fit the hour, so
the laptop won on execution certainty.

The cost of that host, from the repo's own pinned baselines:

| path | p50 | p95/p99 | loss |
|---|---|---|---|
| `media.twiliocdn.com` from CDMX | 55.3 ms | 89.3 ms | **4.0 %** |
| `media.twiliocdn.com` from us-east-1 | 2.9 ms | 7.0 ms | 0 % |
| ngrok ingress (Q-TLS) | 16.7 ms | **226 ms** p99, 61 stalls | — |
| TLS-on-box ingress (Q-TLS) | 20.0 ms | **20.1 ms** p99, 0 stalls | — |

**The failure was the timing of discovery, not the choice.** Every fact above was
knowable at prep time in about twenty seconds. So: run the box check FIRST, and
make the box the default execution host.

A second, independent blocker was found only in hindsight and is now checked:
TLN's config bound **8093**, and the box's Caddy routed only `/e/8080`–`/e/8085`
and `/e/8099`. Even with a fresh binary, Twilio's handshake would have hit the
`respond 404` catch-all and no media would ever have arrived.

## Scope guard — run this before anything else

**PHONE BRIDGES ONLY.** If the event has no participant dial-in:

1. Check the pasted text for a dial-in number.
2. Check `configs/dial-ins.toml` for the issuer's `tier` and `number_src`.
3. Check `state/hunt/numbers.env` for a `<KEY>=` line.
4. Optionally run `python3 scripts/hunt-dial-in.py --key <KEY>`.

If none of those yields a participant number, **say so and stop.** Do not
silently build a webcast lane — that is a different lane with different tools
(`scripts/slate-stage.sh`, `scripts/install-t0-agents.sh`,
`scripts/arm-on-url.sh`) and a different latency story. Offer it; do not assume
it.

---

## Phase 1 — INTAKE

Parse from the pasted confirmation:

- **number(s)** — often dozens of international lines
- **PIN / passcode** and its DTMF form (usually `<pin>#`)
- **call time** and its stated zone
- **issuer / ticker**

**Pick the number for a us-east-1 origin**: US toll-free first, then a US local
(NY/NJ preferred), never an international line. Say which you picked and why —
the arm dials from Twilio's US edge, and an international leg adds PSTN transit
for no benefit. Record the rejected alternatives in one line so the choice is
auditable.

**Confirm the call time against an independent source.** Registration pages
render times in mislabeled zones. Cross-check at least one of:

- the Kalshi event — `python3 scripts/board-open.py --issuer <T> --json`,
  `python3 scripts/discover-events.py`
- the IR release / 8-K that `configs/dial-ins.toml` cites in `number_src`
- `slate/latest-timed.json`

**Flag any disagreement instead of picking one.** A 30-minute error puts the
fire time after the call starts, and the T-90..T-30 probe window lands nowhere.

DTMF charset is `[0-9#w]` — `w` is a 0.5 s wait. **`*` is never permitted**
(`scripts/twilio-dial.sh` and `scripts/event-autorun.sh` both reject it): star
codes raise a hand or unmute, and the posture is listen-only.

## Phase 2 — SECRETS

`configs/*.env` **are tracked in git** (18 of them). `state/` is gitignored.

Write the digits **only** to `state/hunt/numbers.env`, one line per venue:

```
<KEY>=+1XXXXXXXXXX,<pin>#
```

`chmod 600`. The tracked event env reads them at source time, so preflight's
dial-in gate is satisfied without a PIN entering a commit —
`scripts/phone-lane-derive.py` emits that form, including the
laptop-vs-box path resolution and the `TBD` fallback that keeps an unresolved
lookup from sailing through the gate as "resolved".

Then **prove** it:

```
git diff && git status --porcelain          # no digits in any tracked file
python3 scripts/doc-lint.py                 # includes validate_dial_in_digits
```

Never put digits in `admission_detail` / `admission_note` (not digit-safe keys),
in a command line (visible in `ps`), or in a probe transcript that gets committed
(`corpus/bridge-probes/*.txt` is gitignored precisely because IVRs read PINs
aloud).

## Phase 3 — BOX READINESS (early, and loud)

```
scripts/box-ready.sh --config <cfg> --account ~/.earshot/<acct>.env
```

Read-only. One `GO` / `NO-GO: <blocker>` line, plus
`state/<EVENT_ID>/box-ready.json`. It checks source sync, **binary provenance**
(is the box binary newer than every source?), `MENTION_FILL_CAP_CENTS` agreement,
the config's cap arithmetic, Linux ORT, models, Caddy + cert + **the /e/&lt;port&gt;
route**, credentials, disk, clock/NTP, lingering, and the scheduler's own PATH.
See `references/BOX-CHECKS.md` for what each one is defending against.

On NO-GO:

```
scripts/box-ready.sh --config <cfg> --repair
```

`--repair` **refuses while `src/`, `Cargo.toml`, or `Cargo.lock` are dirty** —
AGENTS.md requires deployment to identify an immutable commit, and a dirty rsync
produces a box binary no commit describes (preflight can then only record
`commit=UNKNOWN-NOT-A-GIT-CHECKOUT`). Commit first, then repair.

**If the box cannot be made ready, say so explicitly and early.** Do not fall
back to the laptop without the user's explicit consent; if they choose it, the
laptop path needs `EARSHOT_TUNNEL_REHEARSAL=1` for preflight's TLS gate and pays
the latency in the table above.

## Phase 4 — CONFIG

```
python3 scripts/phone-lane-derive.py --from configs/webcast-<x>-arm-wc.toml
```

Produces a phone-only arm plus its autorun env. What it guarantees:

- exactly one `[[sources]]` `kind = "twilio_stream"`, with `max_reconnects = 2`
  and `idle_timeout_secs = 8` (Q-REDIAL). These require `EARSHOT_REDIAL_DIR`,
  which `scripts/launch-event.sh` exports; without them preflight FAILs the
  config unless launched `--one-shot-ok`.
- **no `[arbiter]`** — `fanout` requires ≥2 sources (`src/config.rs:704`), and
  adding the section to a one-source config fails validation outright.
- caps read from the code, never from memory: `max_price_cents ≤
  MENTION_FILL_CAP_CENTS`, and `max_price_cents × budget_contracts ≤
  [risk].max_order_notional_cents` — get the second wrong and **every** order
  returns `Refusal::OrderNotional`.
- a bind port the ingress actually routes.
- keyword blocks byte-identical to the twin, so `keywords_sha256` is unchanged
  and the existing readiness study + dead-word ack carry over instead of forcing
  a laptop-only `say`-based restudy on event morning.

If the config sets `min_confidence_confirmed`, **label its provenance honestly**.
The current 0.22 came from a single in-sample day (26AUG05 tape, n=26) and is
**not validated** out of sample. The generator writes that in the config header;
do not quietly upgrade the claim.

If there is no webcast twin, start from `earshot prepare --event-ticker <T>` (add
`--fillable` for fill-posture caps) and derive from its output.

## Phase 5 — GATES

Drive the repo's own chain to green. Do not reimplement any of it, and **never
pipe a gate through `head`/`tail`** — that masks `$?` and manufactures green.

```
scripts/variant-study.sh <cfg>                       # macOS only (uses `say`)
#   any keyword with confirmed:0 → widen variants and restudy, or write
#   configs/.acks/<base>-dead-word.ack naming each token. NO THIRD STATE.
scripts/preflight-event.sh --config <cfg> --account <env> \
    --fire-hhmm <HHMM> --event-env <env-file>
scripts/assert-ready.sh <cfg>
```

`preflight-event.sh` runs 22 gates including the short-token (k≤2) ack, the
market-phrase screen, allowlist-vs-live-board, the TLS route probe, the admission
gate, and the mailer — and it **builds `--release` and pins the binary** into
`state/<E>/earshot-pinned`. On the box, run it **on the box**; that is where the
pin has to exist.

Two hashes with deliberately different scopes:

- `config_sha256` covers **the whole file, byte for byte**. Editing a comment
  invalidates the preflight verdict and forces a re-run.
- `keywords_sha256` covers **ticker + phrase + variants only** — deliberately
  **not** caps or thresholds, so retuning a cap does not invalidate a TTS study.

Order matters on Linux: preflight's fire-mechanism gate FAILs when no systemd
timer named `<LABEL>.timer` exists. Either install the timer first (Phase 8) and
then preflight, or expect that one FAIL and re-run preflight after installing.

## Phase 6 — ADMISSION (Q-ADMIT)

**THE MSFT RULE: `admission` is set only by a real probe dial, never from an IR
page.** MSFT's PR said "no password required", the registry said
`listen_access="yes"`, and the live bridge was an operator screen that dropped a
silent caller in 42 s.

Many bridges are **reservation-windowed** — they do not exist outside their event
window. An off-window probe classifies **nothing**. On 2026-08-05 a T-3h09m probe
of TLN got *"Please enter your dial-in pin and press pound when finished. The
conference is not yet available. Please reconnect closer to the scheduled start
time."* Recording that as `dropped` would have permanently failed the gate on
false evidence.

```
scripts/admission-window-probe.sh --config <cfg>            # plan only, no call
scripts/admission-window-probe.sh --config <cfg> --run      # T-90..T-30 only
```

It refuses outside the window (exit 4) and exits 3 when the venue has no number
on file. It does **not** pass `--promote`; to have the probe write its own
verdict, call the prober directly:

```
scripts/bridge-probe.sh --to +1… --dtmf '<pin>#' --label <iss>-<YYYY-MM-DD> \
    --secs 120 --bind-port 8099 --account <env> --promote --issuer <T>
```

`--promote` auto-writes **only** `held`, and only when the probe held ≥95 % of
its cap. Then **read the registry back, never stdout** — on 2026-08-05
`registry-admit.py` hit a `tomllib` ImportError under launchd's minimal PATH, the
promotion silently did not happen, and the verdict had to be recorded by hand.

Outcomes and what each means:

| verdict | preflight | what to do |
|---|---|---|
| `held` | PASS (WARN if >30 d old) | arm |
| `dropped` | FAIL | do not arm until the drop cause is understood |
| `operator_screened` | FAIL unless double-gated | Phase 7 |
| `passcode_required` | FAIL | human records it by hand; needs the transcript to tell it from `dropped` |
| untested + `reservation_window = true` | **WARN**, naming the window | probe in-window; arming is still possible |
| untested, no `reservation_window` | FAIL | probe it — one ~$0.02 call decides it |

If the venue is window-only, set `reservation_window = true` in
`configs/dial-ins.toml`. That is the designed path — it turns the untested FAIL
into a WARN that names the window and the exact command, so no override is
needed.

## Phase 7 — OPERATOR-GATED BRANCH (the only case the user is required)

`admission = "operator_screened"` means a live human screens callers and
disconnects silent lines in under 90 s. Unattended listen-only is infeasible.

The gated human-first ladder exists and its wire facts are **pinned**: Q-CONF
(2026-07-30) proved a `<Start><Stream track="inbound_track">` fork keeps carrying
the bridge's audio while the call sits in `<Dial><Conference>`, **and** that the
paged human's voice stays off the inbound track. Q-UPDATE proved mid-call TwiML
replacement does not kill the fork.

Verify **both** halves of the double gate actually hold before promising it:

1. the event config carries `[operator_handler] enabled = true` with a valid
   E.164 `cell_number`, and
2. `configs/dial-ins.toml` carries `operator_handler = "rehearsed-YYYY-MM-DD"`
   for that venue — set **only** by a passed fake-operator rehearsal, never by
   hand. (Today only YUM and AMD carry it.)

If either is missing, preflight FAILs and you must say so plainly rather than
override.

When it holds, set `OPERATOR_HANDLER=1` in the event env. `launch-event.sh`
pages the cell into the conference **first** and refuses to dial the bridge if
the page is not answered (`operator-page.sh --wait-joined`, exit 7).

**Tell the user the exact UTC minute they must be reachable.** That is the fire
time plus the ring: with `FIRE_HHMM = call − 25 min`, the page goes out within a
minute or two of the fire. State it as a clock time, not as an offset.

## Phase 8 — SCHEDULE + LAUNCH

```
scripts/box-event-install.sh --event-env configs/<ev>-phone-autorun.env
```

It pushes `state/hunt/numbers.env` over **stdin** (0600, never argv), checks that
the config's `bind`, the env's `BIND_PORT`, and the `/e/<port>` in
`EARSHOT_STREAM_URL` all agree, then installs the systemd **user** timer via
`scripts/install-event-timer.sh`.

Dial retry is generous on purpose. **The bridge answers while unprovisioned and
then hangs up**, so a too-early dial looks *successful* to Twilio and dies — TLN
played "not yet available" and cut at 13.1 s. What carries across the
provisioning moment is dial attempts plus mid-call reconnects, so the generated
env sets `DIAL_ATTEMPTS=5` and `STREAM_CONFIRM_SECS=150` (defaults 3 / 90). Size
the no-stream watchdog wider than the redial cycle or a legitimate redial gets
declared a deaf call.

`scripts/event-autorun.sh` then owns the run unattended: up to 25 attempts with
three DTMF timing variants, mail on launch/failure/summary, a mid-call STOP
watcher, and self-disarm.

## Phase 9 — VERIFY BEFORE TRUSTING

**Editing a unit file does not update the loaded job.** `box-event-install.sh`
re-reads the *installed* unit (`systemctl --user cat`, `list-timers`) and asserts
the event env, the ExecStart, the `OnCalendar` minute, and the posture. It pages
and exits 1 on any mismatch.

To prove the flags actually reach `arm` rather than assuming the command line is
right, run the launcher against a **stub binary that prints its argv**:

```
printf '#!/bin/sh\necho "ARGV: $*" >&2\nexit 0\n' > /tmp/argv-stub && chmod +x /tmp/argv-stub
scripts/launch-event.sh --config <cfg> --account <env> --bind-port <p> \
    --to +15550000000 --no-dial --bin /tmp/argv-stub \
    --i-am-live --allow-mention --fillable
```

`--fillable` requires **both** `--i-am-live` and `--allow-mention`
(`src/main.rs:1062-1067`); a job missing one refuses every order.

## After the call

```
scripts/event-status.sh <EVENT_ID>
python3 scripts/missed-arm-check.py --date YYYY-MM-DD
rsync -av -e "ssh -i ~/.ssh/earshot-useast1.pem" \
  ubuntu@3.212.174.94:~/ear/state/<E>/ state/<E>/box/
```

**Post-event truth is `orders[].send_ts != null`, plus the per-event ledger
delta** — never a report's order count. Counting entries is how a "23 orders
placed" claim was produced when the true number was zero: the 1¢ non-crossing
posture short-circuits before `send_order()`, so records exist with
`send_ts: null`.

```
python3 -c "import json;o=json.load(open('state/<E>/report.json'))['orders'];\
print(sum(1 for x in o if x.get('send_ts')),'sent of',len(o),'records')"
```

The `$1` probe ledger is **per `--state-dir`**, so N concurrent events carry N ×
the nominal cap.

## Rehearsal (no dialing, no ordering)

Validate the mechanism through the **production** path — a timer-fired unit, not
a manual run:

```
EARSHOT_AUTORUN_TEST_DIAL=1 EARSHOT_AUTORUN_TEST_TO=+17627212978 \
  scripts/box-event-install.sh --event-env <env> --test --date <YYYY-MM-DD>
```

`--test` bakes `EARSHOT_AUTORUN_TEST=1` into the unit: dry-run flags (orders
computed, never sent), 2 attempts, and `--no-dial` unless a test target is given.
It refuses `TEST_DIAL=1` without an explicit target so a drill can never ring a
real bridge. Kill it with a **real SIGTERM** (`systemctl --user stop`), not the
STOP file — equivalent-looking manual drills validated the wrong paths before the
2026-07-15 MS run.

For an operator-screened rehearsal, point it at `scripts/fake-operator/`, which
asks the INCOM/Chorus screening questions verbatim.

Component dry-runs: `scripts/phone-lane-derive.py --dry-run`,
`scripts/admission-window-probe.sh --config <cfg>` (plan only),
`scripts/box-ready.sh` (read-only), `scripts/box-event-install.sh --verify-only`.

## What this refuses to do

1. **No webcast lanes.** No participant dial-in ⇒ it stops and says so.
2. **No laptop fallback without explicit consent.** The box is the default host.
   A NO-GO is reported with its blocker; it is never routed around silently.
3. **No admission verdict without a real probe** — and never from an off-window
   probe, which classifies nothing.
4. **No digits in git, in argv, or in a non-digit-safe registry field.**
5. **No unattended dial to an operator-screened bridge** unless both halves of
   the double gate hold and the human has answered the page.
6. **No fire-time compile, no unpinned binary, no gate read through a pipe.**
7. **No `*` in DTMF, ever.**

## The one deliberate inversion

**Fail-closed is wrong for a phone-only event.** With no webcast fallback,
refusing to arm is zero coverage — the worst outcome, not the safe one. Where the
evidence supports it (number proven live, PIN prompt correct, bridge
reservation-windowed rather than screening), arm anyway under an explicit,
**paging** override that records its reason:

```
EARSHOT_GREEN_OVERRIDE="<why this is defensible, in one sentence>" \
  scripts/launch-event.sh …
```

`launch-event.sh` proceeds and pages via `scripts/notify.sh`. The reason string
is mandatory and is the record — never leave it blank, and never use it to paper
over a gate that is telling you something true (a `dropped` bridge, a dead
keyword, a stale binary). Prefer the designed path first: marking the venue
`reservation_window = true` turns the untested FAIL into a WARN with no override
at all.

## Reference

- `references/BOX-CHECKS.md` — every box gate and the incident behind it
- `references/TRAPS.md` — the failure modes that cost real time, with dates
- `CLAUDE.md`, `AGENTS.md` — runtime safety conventions and node ownership
- `VERIFICATIONS.md` — Q-ADMIT, Q-TLS, Q-CONF, Q-UPDATE, Q-REDIAL, Q-BOX
- `docs/OPERATOR-HANDLER.md`, `docs/DIAL-INS.md`, `docs/MULTI-EVENT.md` §8
