# Fake operator — rehearsal harness for the operator handler

`server.py` (stdlib-only Python) serves TwiML that imitates the INCOM/Chorus
live-operator screens observed on 2026-07-29 (VERIFICATIONS Q-ADMIT): greeting
verbatim from the MSFT transcript, a spoken name question, a company question,
then an admission announcement followed by the TTS-rehearsal keyword script
(gaming / Hollywood / competition / ad-supported / acquisition) and a long
`<Pause>`. A `Gather` that times out with no speech branches to the real
operators' behavior: one "Hello? Anyone on the line, are you on mute?", 15 s
of grace, an announced disconnect, `<Hangup/>`.

The whole operator ladder — screen → human (v1) or clips (v2) → admission →
detection — rehearses end-to-end against our own number for pennies
(docs/OPERATOR-HANDLER.md).

## Wire a Twilio number at it

1. Run the server on the box: `python3 scripts/fake-operator/server.py --port 8100`
2. Expose it over TLS. Twilio webhooks require a public HTTPS URL:
   - **Box TLS ingress** (production posture, `scripts/install-tls-ingress.sh`):
     add a caddy route for the port (the Caddyfile's `/e/<port>` scheme proxies
     WS; webhooks need a plain HTTP proxy route, e.g. `handle /fake-operator/*`
     → `127.0.0.1:8100`), then use
     `https://media.<domain>/fake-operator/` as the base.
   - **ngrok** (laptop rehearsal): `ngrok http 8100` → `https://<host>/`.
3. Point a Twilio number WE OWN (never a real bridge, never the trading
   `from` number's live config) at it: Console → Phone Numbers → the rehearsal
   number → Voice → "A call comes in" → Webhook `https://<base>/` (POST).
4. Sanity check without a call: `curl -s -X POST https://<base>/` must return
   `<Response><Say>Welcome to the conference center…` and `/mute-check` must
   return the hangup branch.

## Rehearsal ladder (docs/OPERATOR-HANDLER.md)

1. **Screen only:** dial the number with `scripts/twilio-operator-dial.sh`
   (fixture config, `--operator-i-am-paged`) after paging yourself with
   `scripts/operator-page.sh --wait-joined 60`. Answer the fake operator's
   questions by voice; confirm you are "admitted" and the keyword script plays.
2. **Capture proof:** run the same ladder with `earshot bridge-probe` on the
   stream URL; the captured WAV must contain the fake operator + keyword
   script and NOT your voice (Q-CONF posture, `scripts/probe-q-conf.sh`).
3. **Full chain:** `launch-event.sh` rehearsal against this number with a real
   SIGTERM kill (the unattended-validation rule in `CLAUDE.md`), detections
   asserted on the keyword script.
4. Only after 1–3: ONE off-hours probe against a real WING-class bridge,
   never near a traded event.

Silence rehearsal: stay mute after answering — the server must mute-check once
and hang up ~15 s later, which is exactly what a real screen does to an
unmanned leg (that timing is why `--wait-joined` exists on the pager).
