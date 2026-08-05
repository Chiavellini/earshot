# Dual-host phone-lane differential — experiment recipe

**Status: PREPARED, NOT RUN. Requires explicit per-event approval to dial.**
Non-interfering scratch dir. Nothing here is wired into any launcher.

## What this settles

After the 26AUG05 TLN BEHI loss we know **371 of 534 ms was in-process** (model_lag 360 ms +
inference 90.5 ms − chunk 80 ms; `state/archive/tln-26aug05-phone/report.json`). The residual
is **163 ms**, and it must cover BOTH our transport disadvantage (ngrok→CDMX vs box) AND the
competitor's own detect→print latency.

So the open question is narrow and worth exactly one measurement:

> **How much earlier would the us-east-1 box have reached `decision_ts` on the same utterance?**

If the answer is <50 ms, the box is a reliability fix (see the 9,395 dropped frames and the
48.7 s `chunk→detect` stall tail) and **not** a latency lever, and all remaining latency budget
belongs to the decode quantum. If it is >150 ms, it competes with a model swap.

## Design (revised — the original had a safety conflict)

The first draft called for a pure recorder on the second host. **That collides with CLAUDE.md's
"Never point a recorder at a tradeable call"** — `bridge-probe`/`chain-capture` refuse against an
open Kalshi board unless `--corpus-only`, and the rule exists because 26AUG04 recorded SPOT/PGR
instead of arming them. A recorder alongside a live arm is *additive*, not a substitute, but the
rule is absolute and the tooling enforces it.

**Revised design: run `arm` on BOTH hosts. No recorder, no new code, no safety conflict.**

| | Host A | Host B |
|---|---|---|
| Machine | us-east-1 box (16 cores, load 0.00) | CDMX laptop + ngrok |
| Posture | `--i-am-live --allow-mention --fillable` (the real arm) | `--i-am-live --allow-mention` **without `--fillable`** — the non-crossing probe posture, which *cannot* place an order |
| Purpose | coverage, as normal | measurement only |

Both dial the same bridge as separate Twilio calls, run the identical config and binary, and
write wallclock `decision intent` lines. Both hosts are NTP-disciplined (`box-ready.sh` reports
**0 s skew** between them), so the wallclock difference per keyword **is** the answer.

This measures transport *and* contention together — deliberately. That is the decision-relevant
quantity: "would the box have decided sooner," not "what is the pure one-way media delay."

### To separate transport from contention (optional, later)

Only worth doing if the combined number is large. Use a **non-tradeable** call — the fake-operator
number, or any venue with no open Kalshi board — and run `bridge-probe --corpus-only` on both
hosts, then cross-correlate the two `.wav` files. No board, no rule conflict.

## Prerequisites (all prep-time, none on event day)

`scripts/box-ready.sh` currently returns **NO-GO, 5 blockers**:

1. source out of sync — 59 run-affecting paths
2. binary stale (2026-08-04) — the exact TLN blocker
3. `MENTION_FILL_CAP_CENTS`: box 50¢ vs tree 97¢
4. config cap 97¢ > box ceiling 50¢
5. **TLS ingress does not route `/e/8093`** — Twilio's handshake would hit Caddy's 404 catch-all
   and no media would ever arrive. Routed today: 8080–8085, 8099.

Blockers 1–4: `scripts/box-ready.sh --repair` (commits first; refuses a dirty tree).
Blocker 5: bind the config to an already-routed port (**8084 or 8085**), or add the route to
`deploy/caddy/Caddyfile` and re-run `scripts/install-tls-ingress.sh`.

**Re-run `box-ready.sh` until it exits 0 before the event, not on the day.** The whole point of
that script is that this was knowable at prep time and wasn't checked until T-60.

## Run (per event, with approval)

```bash
# 0. prep, days ahead — must exit 0
scripts/box-ready.sh --config <cfg> --account ~/.earshot/<a>.env      # repeat until GO

# 1. box leg (the real arm) — port must be one Caddy routes
scripts/box-event-install.sh --event-env configs/<event>-autorun.env

# 2. laptop leg (measurement only — NOTE: no --fillable)
scripts/launch-event.sh --config <cfg> --account ~/.earshot/<b>.env \
  --bind-port 8093 --to +1XXXXXXXXXX --dtmf <pin># \
  --i-am-live --allow-mention

# 3. after the call
python3 pm-expansion-exploration/latency/dualhost/compare_hosts.py \
  --box   <box-state>/launch.log \
  --local state/<EVENT>/launch.log
```

## Read the result

`compare_hosts.py` pairs `decision intent` / `detection ignored` lines by keyword id across the
two logs and reports the per-utterance wallclock delta.

- **Δ > 0** — the box decided first by that many ms. That is the recoverable transport+contention
  term, measured, on a real call.
- **Δ ≈ 0** — the box is a reliability fix only. Put all remaining effort into the decode quantum
  (P1 chunk-16, then the 80 ms-quantum family).
- **Δ < 0** — the box is slower; investigate before deploying anything.

Also compare `drops.source_to_preproc` and the `chunk→detect` p99/max in the two `report.json`s.
The laptop's were 9,395 and 48.7 s. If the box's are ~0, that is the reliability case closed
independent of whatever Δ turns out to be.

## Cost

One extra Twilio call. Both hosts already provisioned. No code changes.
