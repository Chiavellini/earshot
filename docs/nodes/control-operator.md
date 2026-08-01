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
to the config's `cell_number`, `--wait-joined` answered-poll), the Phase-0 wire
pins `scripts/probe-q-conf.sh` (Q-CONF) and `scripts/probe-q-update.sh`
(Q-UPDATE), the rehearsal harness `scripts/fake-operator/` (stdlib TwiML server
imitating the INCOM/Chorus screens), the invariant test
`tests/twilio_operator_gated.rs`, and the program spec `docs/OPERATOR-HANDLER.md`.

## Does not own

The default listen-only dial (`scripts/twilio-dial.sh`, `control.phone_session`
— must remain byte-identical and is re-frozen by this node's own test), the
`[operator_handler]` config schema and its validation (`runtime.config`,
`src/config.rs`), Twilio media decoding (`runtime.source.twilio`), admission
classification (`control.admission`), redial/supervision policy
(`control.supervise`), and alert delivery (`platform.notify` — this node only
invokes it).

## Input contracts

`control.event.admitted` — the venue must be probed `operator_screened`
(VERIFICATIONS Q-ADMIT) before this path is ever preferred over plain
listen-only. `runtime.source.twilio.listener_ready` — the bridge leg is dialed
only after the event's WS listener owns its bind (same ordering rule as
`control.phone_session`). `platform.notification` — the pager fires a loud
alert alongside the cell call when `scripts/notify.sh` is present (absence is
tolerated, never silent-skipped without a warn).

## Output contracts

`control.operator.attached` (event, compatibility): the operator bridge leg is
admitted to the venue conference with the media fork live. Today it is
observed via the dial scripts' exit statuses plus the `--sid-file` CallSid
files (bridge leg + pager leg); no typed artifact exists yet. Both scripts are
double-gated: `[operator_handler] enabled = true` in the event config AND the
explicit `--operator-i-am-paged` CLI flag; either gate closed is a refusal,
never a fallback to a different TwiML.

## Side effects and exclusive resources

Two paid outbound Twilio calls (bridge + user's cell), DTMF passcode entry
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

## Parallel work boundary

This node may change its own scripts, harness, test, and
`docs/OPERATOR-HANDLER.md`. It must never edit `scripts/twilio-dial.sh` or
`tests/twilio_listen_only.rs` (frozen default path), `src/config.rs` (schema
changes go through `runtime.config`), or the supervision scripts. Consuming a
new readiness signal or emitting a typed attached-artifact requires
coordination with `runtime.assembly` / `control.supervise`.

## Verification

`cargo test --test twilio_operator_gated` (double gate, TwiML shapes, pager
destination pin, DTMF guard, and a re-freeze of the default dial);
`bash -n` on all four scripts; `python3 -m py_compile
scripts/fake-operator/server.py`; `cargo test --test twilio_listen_only`
(unchanged default path stays green). Wire evidence lands in
`corpus/bridge-probes/` and is pinned as Q-CONF / Q-UPDATE sections in
`VERIFICATIONS.md` (snippets printed by the probe scripts).

## Migration status

v1 (this node, human-first) is built and gated: user's cell is paged into the
conference BEFORE the bridge dial, the human answers the operator, hangs up
after admission, and the bridge leg's `inbound_track` fork feeds the normal
pipeline for the whole call. Phase-0 pins Q-CONF and Q-UPDATE have runnable
probe scripts but have NOT been executed yet — no live operator dial is
permitted until both are pinned in `VERIFICATIONS.md` and the fake-operator
rehearsal ladder (see `scripts/fake-operator/README.md`) has passed. v2 per
`docs/OPERATOR-HANDLER.md` — pre-recorded clip responder + intent classifier +
human fallback, and the Q-CONNECT duplex-stream pin — replaces the human-first
answer step later; the double gate, conference topology, and this node's
invariant test carry over unchanged.
