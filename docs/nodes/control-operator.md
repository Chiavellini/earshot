# Node: `control.operator`

## Job

Attach a gated operator-capable phone leg that conferences the configured
human cell into a screened bridge call without touching the default
listen-only path.

## Owns

The operator-gated dial and conference behavior: bridge-leg TwiML construction
in `scripts/twilio-operator-dial.sh` (`<Start><Stream track="inbound_track">` +
`<Dial><Conference startConferenceOnEnter="false" endConferenceOnExit="true">`),
the pager leg in `scripts/operator-page.sh` (`<Say>` + `<Dial><Conference
startConferenceOnEnter="true" endConferenceOnExit="false">`, destination pinned
to the config's `cell_number`, `--wait-joined` answered-poll), its standalone
`--assert-admitted --state-dir ...` explicit operator-pass action, the Phase-0 wire
pins `scripts/probe-q-conf.sh` (Q-CONF) and `scripts/probe-q-update.sh`
(Q-UPDATE), the rehearsal harness `scripts/fake-operator/` (stdlib TwiML server
imitating the INCOM/Chorus screens), the invariant test
`tests/twilio_operator_gated.rs`, and the program spec `docs/OPERATOR-HANDLER.md`.
`scripts/operator-redial.sh` is the bounded replacement wrapper: two 45-second
page attempts by default, with any failed/ringing page positively hung up
before the second is originated, followed by exactly one bridge origination
only after a page joins. A page failure with no CallSid, or a failed page
hangup, is ambiguous and therefore terminal rather than blindly retried.

## Does not own

The default listen-only dial (`scripts/twilio-dial.sh`, `control.phone_session`
— must remain byte-identical and is re-frozen by this node's own test), the
`[operator_handler]` config schema and its validation (`runtime.config`,
`src/config.rs`), Twilio media decoding (`runtime.source.twilio`), admission
classification (`control.admission`), redial/supervision policy
(`control.supervise`), and alert delivery (`platform.notify` — this node only
invokes it).

## Input contracts

`control.event.admitted` — a venue already probed `operator_screened`
(VERIFICATIONS Q-ADMIT) takes this path before its initial bridge dial. For an
absent/`untested`/`unknown` verdict, `control.supervise` may instead select the
same capability only after the first qualifying early drop; that selection is
not itself an admission verdict. `runtime.source.twilio.listener_ready` — the
bridge leg is dialed only after the event's WS listener owns its bind (same
ordering rule as `control.phone_session`). `platform.notification` — the pager
fires a loud alert alongside the cell call when `scripts/notify.sh` is present
(absence is tolerated, never silent-skipped without a warn).

The explicit pass action additionally consumes the live runtime's durable
`admission.challenge.json` and current `stream.connected`. It requires the
challenge event to match the config, the challenge owner PID to be alive, the
challenge's receipt/stream paths to name the selected state directory, and a
valid current Twilio CallSid + nonnegative stream attempt. Page answer, call
creation, stream creation, registry belief, and human hangup are deliberately
not admission inputs.

## Output contracts

`control.operator.attached` (event, compatibility): the pager and operator
bridge legs are attached to the same Twilio conference with the media fork
live. It is observed via dial-script exits plus bridge/page CallSid files; this
does **not** assert that the venue's human screener admitted the call. Both scripts are
double-gated: `[operator_handler] enabled = true` in the event config AND the
explicit `--operator-i-am-paged` CLI flag; either gate closed is a refusal,
never a fallback to a different TwiML.

After the human actually hears the live issuer call, this explicit command is
the `runtime.decision.admission` producer:

```sh
scripts/operator-page.sh --config <event.toml> --operator-i-am-paged \
  --assert-admitted --state-dir <event-state-dir>
```

It first appends and fsyncs one canonical row to
`admission.assertions.jsonl`, then atomically publishes and directory-fsyncs
schema-1 `admission.receipt.json` containing the current challenge event/nonce
and current stream CallSid/attempt. Repeating the same assertion is idempotent.
A redial changes the stream marker, so runtime closes the old receipt and the
replacement leg requires a new explicit assertion.

## Side effects and exclusive resources

One paid bridge call plus one pager call (or two sequential pager calls when
the first is closed before retry), DTMF passcode entry
(charset `[0-9#w]`, `*` hard-rejected — same guard as the default dial), a
named Twilio `<Conference>` room (namespaced `<conference_prefix>-…`, exclusive
to one event), CallSid files, and one notify.sh invocation. The probe scripts
additionally bind a local WS port, may start an ngrok agent, and write WAV/
JSONL/JSON artifacts under `corpus/bridge-probes/`; their destinations are
hard-allowlisted (the TTS rehearsal number `+17627212978` and the configured
`cell_number`; no override flag exists) and they refuse while any armed event
process is live.

## Failure contract

Every gate failure is a loud refusal with a distinct nonzero exit: missing
`--operator-i-am-paged` or a closed config gate (5), a pager `--to` that is not
the configured cell (6), DTMF charset violations (3), a forbidden verb in
constructed TwiML (6, fail-closed self-check). `operator-page.sh
--wait-joined` exits 7 when the human did not answer — the orchestrator must
then NOT dial the bridge; there is no silent retry. Origination failures print
the Twilio response and preserve any known CallSid for
`scripts/twilio-hangup.sh`.
The redial wrapper may retry only the pager leg: exactly two bounded 45-second
answer windows, with the second available only after closing the prior known
CallSid. It never retries bridge origination in place; that preserves the
one-replacement-leg rule when an API outcome is ambiguous.
The pass action exits 8 and writes no receipt for a missing/malformed challenge,
wrong event/path, dead challenge owner, invalid stream marker, or unreadable
existing receipt. A durable assertion row without a receipt (crash between the
two writes) remains fail-closed and can be safely asserted again.

## Parallel work boundary

This node may change its own scripts, harness, test, and
`docs/OPERATOR-HANDLER.md`. It must never edit `scripts/twilio-dial.sh` or
`tests/twilio_listen_only.rs` (frozen default path), `src/config.rs` (schema
changes go through `runtime.config`), or the supervision scripts. Consuming a
new readiness signal or emitting a typed attached-artifact requires
coordination with `runtime.assembly` / `control.supervise`.

## Verification

`cargo test --test twilio_operator_gated` (double gate, TwiML shapes, pager
destination pin, DTMF guard, explicit current-stream admission binding,
reconnect invalidation/idempotency, refusal fixtures, and a re-freeze of the default dial);
`bash -n` on all four scripts; `python3 -m py_compile
scripts/fake-operator/server.py`; `cargo test --test twilio_listen_only`
(unchanged default path stays green). Wire evidence lands in
`corpus/bridge-probes/` and is pinned as Q-CONF / Q-UPDATE sections in
`VERIFICATIONS.md` (snippets printed by the probe scripts).
`tests/operator_escalation.sh` supplies offline page/hangup/bridge stubs to the
real redial wrapper and verifies the bounded retry ordering, single bridge
attempt, ambiguous-page refusal, and absence of auto-minted admission proof.

## Migration status

v1 (this node, human-first) is built and gated: user's cell is paged into the
conference BEFORE the bridge dial, the human answers the operator, hangs up
after admission, and the bridge leg's `inbound_track` fork feeds the normal
pipeline for the whole call. Q-OPERATOR-V1, Q-CONF, and Q-UPDATE are pinned in
`VERIFICATIONS.md`; the registry's `operator_handler = "rehearsed-<date>"` is
the machine gate consumed by supervision. The explicit pass receipt keeps
decisions closed until the human confirms the real venue admission. v2 per
`docs/OPERATOR-HANDLER.md` — pre-recorded clip responder + intent classifier +
human fallback, and the Q-CONNECT duplex-stream pin — replaces the human-first
answer step later; the double gate, conference topology, and this node's
invariant test carry over unchanged.
