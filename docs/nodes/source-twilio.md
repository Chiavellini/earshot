# Node: `runtime.source.twilio`

## Job

Accept one authenticated Twilio Media Stream and decode inbound G.711 µ-law
messages into telephony `AudioFrame`s.

## Owns

WebSocket bind/accept, `X-Twilio-Signature` validation, Twilio message
validation, µ-law decoding, reconnect windows, and redial request markers in
`src/source/twilio_ws.rs`, with wire/operator behavior documented in
`docs/TWILIO.md`.

## Does not own

It does not place or hang up calls, choose DTMF, expose TLS, supervise redial,
arbitrate sources, validate markets, or define the shared source port.

## Input contracts

`TwilioWsSource::bind(addr, source_id)` reserves the listen socket.
Assembly resolves the primary per-event `[twilio].auth_token_path` and
`[twilio].stream_url` binding and pins it with `with_auth`; environment
variables are a compatibility fallback. The accepted stream must declare mono
`audio/x-mulaw` at 8 kHz and follow Twilio's start/media/stop sequencing.
Adapter construction does not consume `control.phone.attached`; that event
exists only after a ready listener has accepted the dialed call.

## Output contracts

Each valid media message becomes ordered PCM `AudioFrame` data stamped on
network arrival. Clean stop returns `EndOfStream`; configured recoverable drops
atomically refresh `<redial-dir>/redial.request` and reopen the accept window.
The marker body is the pinned supervisor wire contract:
`{"unix_ts":<int-seconds>,"old_call_sid":"<sid-or-empty>","attempt":<int>,
"reason":"stop"|"idle"|"socket_closed"|"close_frame"|"ws_error"}` (`reason` is
the `StreamEnd` variant that triggered the redial). With `max_reconnects > 0`
the read loop also `tokio::select!`s on the listener while a stream is live: a
signature-validated new connection whose `start` carries a DIFFERENT callSid
is the redial landing (`StreamEnd::Superseded`) — the source swaps to it
immediately, dropping the half-open predecessor, consuming no reconnect
attempt and writing no marker. A same-callSid connection is a stale zombie and
is dropped (StaleReattach semantics). Every accepted TCP stream (first accept
and re-accepts) gets `TCP_NODELAY` before the WS handshake.

## Side effects and exclusive resources

Exclusively owns its TCP bind address and accepted WebSocket. It reads Twilio
signature secrets and may write `redial.request`; it does not write call
credentials or place calls.

## Failure contract

Bind conflicts fail at construction. Bad signatures are rejected before
upgrade; invalid media format, malformed protocol, accept timeout, and idle
timeout fail loudly. Missing auth is an explicit dev-only warning, never an
implicit production success. A mid-stream supersede candidate is bounded (2 s
handshake + 2 s start-wait; while either step runs the live stream is briefly
unread) and any candidate failure — bad handshake, malformed or wrong-format
start, timeout — drops only the candidate, never the live stream.

## Parallel work boundary

Twilio media behavior stays in `src/source/twilio_ws.rs` and Twilio source
tests. Dialing belongs to control nodes; changing the redial marker schema
requires the supervisor contract.

## Verification

Run `cargo test --test twilio_source` (marker schema, supersede swap/attempt
accounting, one-shot isolation regressions included),
`cargo test --test twilio_listen_only`, and the unit tests in
`source::twilio_ws`. Live wire facts remain pinned in `docs/TWILIO.md` and
`VERIFICATIONS.md`.

## Migration status

The media adapter and per-event signature binding are implemented. The redial
handoff remains a file convention, and dialing remains coupled through
`scripts/launch-event.sh` and `scripts/redial-supervisor.sh`. Replace the marker
with a versioned `RedialRequest` artifact or channel.
