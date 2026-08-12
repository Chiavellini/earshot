#!/usr/bin/env bash
# Page the USER'S CELL into an event's operator conference (docs/OPERATOR-HANDLER.md
# v1, human-first). Originated BEFORE scripts/twilio-operator-dial.sh dials the
# bridge, so the operator's questions are answered by a live human — never by
# silence (Class B bridges drop silent callers in 21-73 s, VERIFICATIONS Q-ADMIT).
#
#   operator-page.sh --config <event.toml> --operator-i-am-paged \
#                    [--conference <name>] [--sid-file <path>] \
#                    [--wait-joined <secs>] [--dry-run]
#
# After the human has actually passed the screener (separate explicit action):
#   operator-page.sh --config <event.toml> --operator-i-am-paged \
#                    --assert-admitted --state-dir <event-state-dir>
#
# DESTINATION IS NOT A CHOICE: the pager dials EXACTLY the config's
# [operator_handler] cell_number. A --to arg is accepted only as a tripwire —
# any value other than the configured cell is refused.
#
# TwiML (the pager invariant, tests/twilio_operator_gated.rs):
#   <Say>   allowed HERE ONLY — it targets the user's own phone, never a bridge
#   <Dial>  contains ONLY <Conference> (startConferenceOnEnter="true": media
#           starts when the human answers; endConferenceOnExit="false": the
#           human hanging up after admission must NOT kill the bridge leg)
#   NEVER <Number>, <Sip>, <Connect>, <Record>, <Play digits>.
#
# --wait-joined N polls the Twilio Calls API until the page is answered
# (status=in-progress) and exits nonzero on timeout/busy/no-answer — the
# orchestrator uses this to never dial a bridge unmanned.
set -euo pipefail

CONFIG="" CONFERENCE="" SID_FILE="" DRY_RUN=0 PAGED=0 TO_OVERRIDE="" WAIT_JOINED=""
ASSERT_ADMITTED=0 STATE_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)              CONFIG="$2"; shift 2 ;;
    --conference)          CONFERENCE="$2"; shift 2 ;;
    --sid-file)            SID_FILE="$2"; shift 2 ;;
    --to)                  TO_OVERRIDE="$2"; shift 2 ;;   # tripwire only — must equal the config cell
    --wait-joined)         WAIT_JOINED="$2"; shift 2 ;;   # poll until answered, else exit nonzero
    --assert-admitted)     ASSERT_ADMITTED=1; shift ;;
    --state-dir)           STATE_DIR="$2"; shift 2 ;;
    --operator-i-am-paged) PAGED=1; shift ;;
    --dry-run)             DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" ]] || {
  echo "usage: $0 --config <event.toml> --operator-i-am-paged [--conference <name>] [--sid-file <path>] [--wait-joined <secs>] [--dry-run]" >&2
  echo "       $0 --config <event.toml> --operator-i-am-paged --assert-admitted --state-dir <event-state-dir>" >&2
  exit 2
}
[[ -f "$CONFIG" ]] || { echo "config not found: $CONFIG" >&2; exit 2; }
if [[ -n "$WAIT_JOINED" ]]; then
  [[ "$WAIT_JOINED" =~ ^[1-9][0-9]*$ && "$WAIT_JOINED" -le 600 ]] || { echo "REFUSING: --wait-joined must be an integer 1..600 (secs)" >&2; exit 2; }
fi

# ── DOUBLE GATE (same as twilio-operator-dial.sh) ───────────────────────────
if [[ "$PAGED" != "1" ]]; then
  echo "REFUSING: --operator-i-am-paged not passed (the operator path is explicitly opt-in per dial)." >&2
  exit 5
fi
OP_ENABLED=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*enabled[[:space:]]*=[[:space:]]*\([a-z]*\).*/\1/p' "$CONFIG" | head -1)
if [[ "$OP_ENABLED" != "true" ]]; then
  echo "REFUSING: ${CONFIG} has no [operator_handler] enabled = true." >&2
  exit 5
fi

# Config fields (same sed style as launch-event.sh; lines are prepare-shaped).
OP_CELL=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*cell_number[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
OP_PREFIX=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*conference_prefix[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
[[ -n "$OP_PREFIX" ]] || OP_PREFIX="earshot"
EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
[[ "$OP_CELL" =~ ^\+[1-9][0-9]{7,14}$ ]] || {
  echo "REFUSING: [operator_handler] cell_number ${OP_CELL:-<empty>} in ${CONFIG} is not strict E.164" >&2
  exit 2
}

# Explicit operator-pass assertion. This is intentionally NOT inferred from a
# page reaching `in-progress`, a bridge call being created, a stream landing,
# or the human hanging up: each can happen while the screener still has not
# admitted the bridge. The human invokes this only after hearing the live call.
#
# The writer binds that assertion to the runtime's durable challenge and the
# CURRENT stream marker. A redial changes call_sid/attempt and automatically
# invalidates the old receipt in runtime assembly, requiring a fresh assertion.
if [[ "$ASSERT_ADMITTED" == 1 ]]; then
  [[ -n "$STATE_DIR" ]] || { echo "REFUSING: --assert-admitted requires --state-dir" >&2; exit 2; }
  [[ "$DRY_RUN" == 0 && -z "$WAIT_JOINED" && -z "$SID_FILE" ]] || {
    echo "REFUSING: --assert-admitted is a standalone action; do not combine it with page/dry-run options" >&2
    exit 2
  }
  EARSHOT_OPERATOR_STATE_DIR="$STATE_DIR" \
  EARSHOT_OPERATOR_EVENT_ID="$EVENT_ID" \
  EARSHOT_OPERATOR_REPO="$(cd "$(dirname "$0")/.." && pwd)" \
  EARSHOT_OPERATOR_ASSERTED_BY="${EARSHOT_OPERATOR_ASSERTED_BY:-human-cli}" \
  python3 - <<'PY'
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time


def refuse(message, code=8):
    print(f"REFUSING admission assertion: {message}", file=os.sys.stderr)
    raise SystemExit(code)


state = Path(os.environ["EARSHOT_OPERATOR_STATE_DIR"])
try:
    state = state.resolve(strict=True)
except OSError as exc:
    refuse(f"state dir is not a real existing directory: {exc}")
if not state.is_dir():
    refuse(f"state path is not a directory: {state}")


def regular_json(name):
    path = state / name
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        refuse(f"missing {name}: {exc}")
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        refuse(f"{name} must be a regular non-symlink file")
    try:
        return path, json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        refuse(f"cannot parse {name}: {exc}")


challenge_path, challenge = regular_json("admission.challenge.json")
stream_path, stream = regular_json("stream.connected")
event_id = os.environ["EARSHOT_OPERATOR_EVENT_ID"]
if challenge.get("schema_version") != 1:
    refuse("unsupported challenge schema")
if challenge.get("event_id") != event_id:
    refuse(
        f"challenge event {challenge.get('event_id')!r} != config event {event_id!r}"
    )
nonce = challenge.get("nonce")
if not isinstance(nonce, str) or len(nonce) != 64:
    refuse("challenge nonce is absent or malformed")
pid = challenge.get("pid")
if not isinstance(pid, int) or pid <= 0:
    refuse("challenge pid is absent or malformed")
try:
    os.kill(pid, 0)
except OSError:
    refuse(f"challenge owner pid {pid} is not alive")


def challenge_path_is(field, expected):
    raw = challenge.get(field)
    if not isinstance(raw, str):
        return False
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = Path(os.environ["EARSHOT_OPERATOR_REPO"]) / candidate
        return candidate.resolve() == expected
    except OSError:
        return False


receipt_path = state / "admission.receipt.json"
if not challenge_path_is("receipt_path", receipt_path):
    refuse("challenge receipt_path does not name this state directory")
if not challenge_path_is("stream_connected_path", stream_path):
    refuse("challenge stream_connected_path does not name this state directory")
call_sid = stream.get("call_sid")
attempt = stream.get("attempt")
if not isinstance(call_sid, str) or not (
    len(call_sid) == 34
    and call_sid.startswith("CA")
    and all(c in "0123456789abcdefABCDEF" for c in call_sid[2:])
):
    refuse("current stream marker has no valid Twilio CallSid")
if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
    refuse("current stream marker has no valid nonnegative attempt")

receipt = {
    "schema_version": 1,
    "event_id": event_id,
    "nonce": nonce,
    "status": "admitted",
    "call_sid": call_sid,
    "stream_attempt": attempt,
    "assertion_method": "explicit_operator_pass_cli",
    "asserted_by": os.environ["EARSHOT_OPERATOR_ASSERTED_BY"],
    "asserted_unix_ns": time.time_ns(),
    "challenge_pid": pid,
}

# Idempotent for the same live challenge/stream: never manufacture multiple
# assertion rows because a user safely repeated the exact command.
try:
    prior = json.loads(receipt_path.read_bytes())
except FileNotFoundError:
    prior = None
except (OSError, json.JSONDecodeError) as exc:
    refuse(f"existing receipt is unreadable; refusing to overwrite: {exc}")
if prior is not None and all(
    prior.get(k) == receipt[k]
    for k in ("schema_version", "event_id", "nonce", "status", "call_sid", "stream_attempt")
):
    print(
        f"operator admission already asserted for event={event_id} "
        f"call_sid={call_sid} stream_attempt={attempt}"
    )
    raise SystemExit(0)

# Durable append-only audit row first. If the process dies before publishing
# the receipt, decisions remain closed but the assertion itself is recoverable.
assertion = dict(receipt)
assertion["kind"] = "operator_admission_assertion"
canonical = json.dumps(assertion, sort_keys=True, separators=(",", ":")).encode()
assertion["assertion_sha256"] = hashlib.sha256(canonical).hexdigest()
line = json.dumps(assertion, sort_keys=True, separators=(",", ":")).encode() + b"\n"
audit_path = state / "admission.assertions.jsonl"
fd = os.open(audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    if os.write(fd, line) != len(line):
        refuse("short write to admission assertion journal")
    os.fsync(fd)
finally:
    os.close(fd)

# Atomic + durable receipt publication. Runtime reads this on every decision;
# only after replace+directory fsync can the explicit assertion open the gate.
encoded = json.dumps(receipt, indent=2, sort_keys=True).encode() + b"\n"
tmp = state / f".admission.receipt.{os.getpid()}.tmp"
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
fd = os.open(tmp, flags, 0o600)
try:
    if os.write(fd, encoded) != len(encoded):
        refuse("short write to admission receipt")
    os.fsync(fd)
finally:
    os.close(fd)
os.replace(tmp, receipt_path)
dirfd = os.open(state, os.O_RDONLY)
try:
    os.fsync(dirfd)
finally:
    os.close(dirfd)
print(
    f"operator admission ASSERTED event={event_id} call_sid={call_sid} "
    f"stream_attempt={attempt}; receipt={receipt_path}"
)
PY
  exit $?
fi
# The pager's destination is the configured cell, full stop.
if [[ -n "$TO_OVERRIDE" && "$TO_OVERRIDE" != "$OP_CELL" ]]; then
  echo "REFUSING: --to \"$TO_OVERRIDE\" != configured [operator_handler] cell_number \"$OP_CELL\"." >&2
  echo "  The pager leg may dial ONLY the configured cell. Edit the config, not the flag." >&2
  exit 6
fi
TO="$OP_CELL"

# Conference name: identical derivation to twilio-operator-dial.sh so both
# legs land in the SAME room from --config alone.
if [[ -z "$CONFERENCE" ]]; then
  [[ -n "$EVENT_ID" ]] || { echo "could not parse [event].id from ${CONFIG} (needed for the default conference name; pass --conference)" >&2; exit 2; }
  SAFE_EVENT=$(printf '%s' "$EVENT_ID" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-')
  CONFERENCE="${OP_PREFIX}-${SAFE_EVENT}"
fi
[[ "$CONFERENCE" =~ ^[a-z0-9-]{1,64}$ ]] || { echo "REFUSING: conference name ${CONFERENCE:0:80}... must match [a-z0-9-]{1,64}" >&2; exit 2; }
[[ "$CONFERENCE" == "$OP_PREFIX"-* || "$CONFERENCE" == "$OP_PREFIX" ]] || {
  echo "REFUSING: --conference \"$CONFERENCE\" is outside this event's namespace \"${OP_PREFIX}-*\"" >&2
  exit 2
}

# <Say> body: EVENT_ID is prepare-shaped ([A-Z0-9-]); anything else is
# sanitized so the XML cannot be broken or repurposed.
SAY_EVENT=$(printf '%s' "${EVENT_ID:-unknown-event}" | tr -c 'a-zA-Z0-9 .-' ' ')
TWIML="<Response><Say>EARSHOT operator screen for ${SAY_EVENT}. Joining you to the bridge conference now. Handle the operator, then hang up.</Say><Dial><Conference beep=\"false\" startConferenceOnEnter=\"true\" endConferenceOnExit=\"false\">${CONFERENCE}</Conference></Dial></Response>"

# Fail-closed self-check: even on the user's own phone, never these verbs.
for forbidden in "<Number" "<Sip" "<Connect" "<Record" "<Play"; do
  if [[ "$TWIML" == *"$forbidden"* ]]; then
    echo "REFUSING: constructed pager TwiML contains forbidden verb ${forbidden}" >&2
    exit 6
  fi
done

# Dry run: TwiML + destination + redacted curl; no call, no creds needed.
if [[ "$DRY_RUN" == "1" ]]; then
  echo "$TWIML"
  echo "TO=${TO}"
  echo "CONFERENCE=${CONFERENCE}"
  echo "# curl -s -X POST https://api.twilio.com/2010-04-01/Accounts/<SID>/Calls.json \\"
  echo "#   --data-urlencode \"To=${TO}\" --data-urlencode \"From=<FROM>\" \\"
  echo "#   --data-urlencode \"Twiml=<above>\" -u \"<SID>:<REDACTED>\""
  exit 0
fi

# Per-event credentials from the caller WIN; global file is the manual fallback.
if [[ -z "${TWILIO_ACCOUNT_SID:-}" || -z "${TWILIO_AUTH_TOKEN:-}" || -z "${TWILIO_FROM_NUMBER:-}" ]]; then
  # shellcheck disable=SC1090
  source ~/.twilio/earshot_env
fi
: "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}" "${TWILIO_FROM_NUMBER:?}"

RESP=$(curl -s -X POST "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls.json" \
  --data-urlencode "To=${TO}" \
  --data-urlencode "From=${TWILIO_FROM_NUMBER}" \
  --data-urlencode "Twiml=${TWIML}" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")

SID=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("sid","ERROR"))')
if [[ "$SID" == "ERROR" || -z "$SID" ]]; then
  echo "pager-leg origination FAILED:" >&2
  echo "$RESP" >&2
  exit 1
fi
echo "pager leg placed: CallSid=${SID} to=${TO} conference=${CONFERENCE}"
[[ -n "$SID_FILE" ]] && echo "$SID" > "$SID_FILE"

# Best-effort loud alert so the phone in a pocket gets a second channel.
# notify.sh may be absent on a stripped box — tolerate, never block the page.
if [[ -x "$(dirname "$0")/notify.sh" ]]; then
  "$(dirname "$0")/notify.sh" "OPERATOR PAGE: answer your cell NOW" \
    "Event ${EVENT_ID:-?}: operator screen. Answer ${TO}, handle the operator, then hang up. Conference ${CONFERENCE}, CallSid ${SID}." \
    || echo "warn: notify.sh failed (page still placed)" >&2
fi

# --wait-joined: the orchestrator's "never dial a bridge unmanned" primitive.
# in-progress = answered; queued/ringing = keep waiting; anything terminal
# (busy / no-answer / failed / canceled / completed) = the page did NOT land.
if [[ -n "$WAIT_JOINED" ]]; then
  DEADLINE=$(( $(date +%s) + WAIT_JOINED ))
  while :; do
    STATUS=$(curl -s "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls/${SID}.json" \
      -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" \
      | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))' || true)
    case "$STATUS" in
      in-progress)
        echo "pager ANSWERED (status=in-progress) — human is in the conference"
        exit 0 ;;
      busy|no-answer|failed|canceled|completed)
        echo "pager NOT joined: terminal status=${STATUS} — do NOT dial the bridge" >&2
        exit 7 ;;
    esac
    if (( $(date +%s) >= DEADLINE )); then
      echo "pager NOT answered within ${WAIT_JOINED}s (last status=${STATUS:-unknown}) — do NOT dial the bridge" >&2
      echo "  hang the page up with: scripts/twilio-hangup.sh ${SID}" >&2
      exit 7
    fi
    sleep 2
  done
fi
exit 0
