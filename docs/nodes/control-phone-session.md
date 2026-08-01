# Node: `control.phone_session`

## Job

Create, observe, and terminate one Twilio phone call only after the matching
media listener is proven ready.

## Owns

Call creation, DTMF/pause construction, CallSid persistence, status lookup, and
hangup behavior in `scripts/twilio-dial.sh`, `scripts/twilio-status.sh`, and
`scripts/twilio-hangup.sh`.

## Does not own

It does not resolve venue access, bind the WebSocket listener, decode Twilio
media, choose redial policy, classify bridge admission, launch the runtime, or
grade the call. The gated operator-capable conference leg — its scripts, its
spec (`docs/OPERATOR-HANDLER.md`), and its invariant test — belongs to
`control.operator`; this node's default dial stays byte-identical regardless
of that path's existence.

## Input contracts

The required entry is a `PhoneSessionRequest` containing listener readiness,
access plan, account reference, and state directory. `listener_ready` is
the `runtime.source.twilio.listener_ready` token — the file
`<state_dir>/listener.ready`
(`{"port":<u16>,"pid":<u32>,"unix_ts":<int-seconds>}`) written by runtime
assembly's arm path only after the detector is fully constructed and the
pipeline thread is spawned; an open port alone is NOT readiness (the listener
binds before the detector builds — the 2026-07-29 ORT hang). The access plan
supplies phone/DTMF secret references, stream URL, pause policy, and deadline.

## Output contracts

Success returns a `PhoneSession` containing run ID, CallSid, status, stream URL,
start time, and cleanup token, and currently writes the CallSid to the requested
state file. The resulting `control.phone.attached` event is consumed by
supervision and telemetry; it is not fed backward into adapter construction.
Status and termination operations require that session identity.

## Side effects and exclusive resources

It makes Twilio API calls, incurs a paid outbound call, sends DTMF, writes one
CallSid file, and may terminate the call. The CallSid, Twilio account/from
number, and destination bridge slot are exclusive to the event attempt.

## Failure contract

A request without matching `ListenerReady` is refused: dial-before-bind is
never retried or reordered. Invalid DTMF, missing credentials, call creation
failure, status failure, or hangup failure is loud and preserves the known
CallSid for reconciliation; ambiguous creation is not blindly repeated.

## Parallel work boundary

Twilio call-control scripts and the phone-session artifact live here. Runtime
assembly owns listener readiness, the Twilio source owns media/protocol handling,
supervision owns redial budgets, and admission owns interpretation of probe
audio.

## Verification

Run `bash -n` on `scripts/twilio-dial.sh`, `scripts/twilio-status.sh`, and
`scripts/twilio-hangup.sh`; validate DTMF guards with non-network fixtures; and
use a bounded listen-only rehearsal to prove listener-ready precedes the API
call and cleanup targets only its CallSid.

## Migration status

The scripts already separate dial, status, and hangup, but callers exchange
stream URLs and CallSids through arguments/files. The readiness token now
exists: `<state_dir>/listener.ready`, written by `main.rs::arm` strictly after
detector construction + pipeline spawn (2026-07-29), and deleted at arm start
so a stale token never green-lights a dial. Launch must gate the dial on this
file, never on a port probe. Remaining: fold the token into typed
`PhoneSessionRequest/PhoneSession` artifacts and make both initial launch and
redial supervision use them.
