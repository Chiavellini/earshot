# Twilio Media Streams — wire facts (M3 capture path)

Same discipline as `VERIFICATIONS.md` but Twilio-scoped: code against what
is pinned here, and upgrade "from docs" pins to "live-verified" pins (with
captured JSONL evidence) during the loopback rehearsal.

## Topology

```
bridge (PSTN) ── Twilio outbound call ── <Start><Stream>
                                              │ wss:// (we are the SERVER)
                              ngrok/public host ──► TwilioWsSource (127.0.0.1:8080)
```

- `scripts/twilio-dial.sh` POSTs to `/2010-04-01/Accounts/{SID}/Calls.json`
  with inline TwiML: `<Start><Stream url track="inbound_track"/></Start>`
  (async fork of the caller-side audio — listen-only), `<Play digits>` for
  the bridge passcode via DTMF (`w` = 0.5 s wait), `<Pause length="5400"/>`
  to hold the line.
- `scripts/twilio-hangup.sh <CallSid>` sets `Status=completed` — the
  audio-side kill switch (orders side is `state/STOP`).

## Request authenticity — `X-Twilio-Signature`

We are a public WS server, so the upgrade request is validated before it
becomes an audio source. Twilio signs each request:
`X-Twilio-Signature = base64(HMAC-SHA1(authToken, streamURL + sorted params))`.
The WS upgrade is a GET with no body params (Media Streams `<Parameter>`s
arrive in the `start` message, not the URL), so the signed string is just the
`<Stream url="wss://…">`. `TwilioWsSource` checks it in the `accept_hdr_async`
handshake callback and rejects a mismatch with 403.
- Token from the config's `[twilio] auth_token_path` (a file) and URL from
  `[twilio] stream_url` when a `[twilio]` section is present — the per-account
  binding for concurrent events (see `docs/MULTI-EVENT.md` §8). Otherwise the env
  fallback: token from `TWILIO_AUTH_TOKEN`, URL pinned by `TWILIO_STREAM_URL` (else
  reconstructed from the `Host` header + request target). Never in repo.
- No token set ⇒ validation skipped with a loud warn (dev/loopback only).
- Scheme is from official docs (pinned in `VERIFICATIONS.md`); confirm the
  reconstructed-URL bytes against a live handshake in the loopback rehearsal.

## Message shapes (source: Twilio Media Streams docs — PENDING live capture)

All messages are JSON over WS text frames. Confidence: from official docs,
2026-07-11; each shape must be re-verified from the captured JSONL of the
first loopback call and this file stamped accordingly.

- `{"event":"connected","protocol":"Call","version":"1.0.0"}` — first.
- `{"event":"start","sequenceNumber":"1","start":{"accountSid":…,"streamSid":…,
  "callSid":…,"tracks":["inbound"],"mediaFormat":{"encoding":"audio/x-mulaw",
  "sampleRate":8000,"channels":1}},"streamSid":…}` — the source REFUSES
  (fail loud) any other mediaFormat.
- `{"event":"media","sequenceNumber":"3","media":{"track":"inbound",
  "chunk":"2","timestamp":"5","payload":"<base64 μ-law>"},"streamSid":…}` —
  nominally 160 bytes = 20 ms per message; `timestamp` is ms since stream
  start; `sequenceNumber` is a stringified integer, consecutive (gaps =
  network loss, warned).
- `{"event":"stop",…}` — clean end; a socket close without it is warned but
  treated as end-of-stream.
- `mark` events exist only for outbound audio — ignored.

## Latency expectations (research 2026-07-11, unmeasured on our path)

PSTN leg ~200–400 ms + Twilio media relay + WS transit ~40–100 ms →
~0.3–0.6 s from utterance to our `capture_ts`. To be measured in the
loopback rehearsal (known WAV played into a real call; compare burst
offsets vs detection wall-clock). Webcasts run 10–30 s behind — that gap is
the strategy's structural edge.

## Loopback rehearsal (must pass before any real call)

1. `ngrok http 8080` (or reserved-domain equivalent) → note wss host.
2. `earshot arm --config <tone-variant config>` (dry-run, stub detector).
3. Upload the fixture WAV (8 kHz μ-law) to Twilio Assets; create a TwiML
   Bin `<Play>` loop on a second number.
4. `twilio-dial.sh --to <that number> --stream-url wss://<host>/media`.
5. Assert 3 tone-burst detections; measure capture latency; capture the raw
   message JSONL and upgrade the pins above.
6. Repeat with sherpa_kws + a spoken-keyword WAV.

## Operational cautions

- Trial accounts restrict outbound calls to verified numbers and inject a
  preamble — upgrade the account before the rehearsal.
- A new-account compliance/fraud review hold can block outbound calling even
  when the read-only Account API still reports `status=active`. Confirm the
  account is actually clear before the rehearsal with
  `scripts/twilio-status.sh` (read-only snapshot) and, definitively,
  `scripts/twilio-status.sh --probe --probe-to +1...` — it places a
  self-hanging-up test call and reports LIFTED (CallSid returned) or STILL
  BLOCKED (Twilio error code). `--watch` polls until the hold lifts.
- Listen-only, real-identity registration where a bridge requires it, and
  NO audio recording anywhere in the pipeline (frames are in-memory only).
  The one exception is `earshot capture-bakeoff --record-dir` — an explicit,
  opt-in ingress diagnostic (docs/INGRESS-BAKEOFF.md), never part of the
  serving path; do not point it at a production trading run.
- ngrok free-tier hostnames rotate per session; grab the current one before
  each dial. Reserved domain (or a VPS) if the rehearsal shows instability.
