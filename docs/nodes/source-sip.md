# Node: `runtime.source.sip`

## Job

Receive an already-negotiated RTP media flow, reorder it, decode G.711 µ-law,
and emit one clean telephony frame stream.

## Owns

UDP receive, RTP parsing, payload validation, jitter buffering, reordering,
idle handling, and PCM emission in `src/source/sip.rs`.

## Does not own

It does not perform SIP REGISTER/INVITE/SDP signaling, select a carrier, dial a
bridge, validate admission, share Twilio's control plane, or define downstream
audio processing.

## Input contracts

`SipRtpSource` receives a bind address, source ID, and jitter-buffer setting
from assembly. The upstream gateway must deliver supported RTP carrying 8 kHz
G.711 µ-law to that socket.

## Output contracts

Valid packets become arrival-stamped, dense-sequence `AudioFrame`s in
de-jittered RTP order. A dead stream flushes eligible buffered packets before
ending according to the shared `AudioSource` contract.

## Side effects and exclusive resources

Exclusively owns one UDP bind address for the run. It receives network
datagrams but opens no signaling connection and starts no carrier session.

## Failure contract

Bind and receive errors return `SourceError::Io`. Datagrams that do not parse
as supported RTP are ignored with debug diagnostics so unrelated UDP traffic
cannot kill the leg; valid packets outside the jitter policy are dropped or
skipped deterministically. Promoting malformed-packet diagnostics to a counted
health signal remains migration work.

## Parallel work boundary

Media-plane changes stay in `src/source/sip.rs` and `tests/sip_source.rs`.
Carrier signaling must be a separate control/acquisition node and may hand over
only a structured RTP endpoint/session description.

## Verification

Run `cargo test --test sip_source` and focused `cargo test source::sip` tests
against local RTP fixtures. A live carrier test is not evidence for parser
correctness unless the packet/artifact inputs are retained.

## Migration status

The carrier-independent media plane exists and is locally testable. Live SIP
signaling is intentionally absent, so current phone production still enters
through Twilio; the desired handoff is a versioned negotiated RTP session
artifact.
