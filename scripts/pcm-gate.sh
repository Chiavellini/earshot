#!/usr/bin/env bash
# pcm-gate.sh — prove a webcast source actually yields DECODABLE AUDIO on THIS host.
#
# WHY THIS EXISTS: every readiness gate we had for the webcast lane checks the
# manifest (HTTP 200 + #EXTM3U). That check is not merely weak, it is
# AFFIRMATIVELY MISLEADING on at least one venue class. HOOD 2026-07-29, the
# first YouTube event we ever armed: the master manifest resolved and returned
# 200 from us-east-1, the arm started clean, and BOTH legs died inside 35 s with
# frames=0 — YouTube bot-walls the datacenter IP at the *segment* layer, so the
# manifest is a decoy that 200s while the media underneath is refused.
# scripts/youtube-resolve.sh:26-31 has said in a comment since that day that the
# only gate which can clear this venue is PCM BYTES. Nothing implemented it.
# This does. Run it per host × per event BEFORE arming.
#
#   scripts/pcm-gate.sh --kind youtube --url https://www.youtube.com/live/<id>
#   scripts/pcm-gate.sh --kind hls     --url-file $HOME/snap-stream.url
#   scripts/pcm-gate.sh --kind hls     --url "https://…/master.m3u8" --secs 8
#
# Contract: 16 kHz mono s16le ⇒ 32000 bytes/s. --secs 6 ⇒ 192000 bytes expected.
# PASS at >= 80% of expected (allows one short read at the join point).
# Exit 0 PASS · 1 FAIL (short/zero bytes) · 2 usage · 3 source unreachable.
#
# A FAIL means THIS VANTAGE IS DEAD for this event. Fall back to another host —
# do not arm hopefully. frames=0 at 35 s is the HOOD signature.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

KIND=""; URL=""; URLFILE=""; SECS=6; LABEL=""; RUN_DIR=""; PROOF_JSON=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kind)     KIND="${2:?}"; shift 2 ;;
    --url)      URL="${2:?}"; shift 2 ;;
    --url-file) URLFILE="${2:?}"; shift 2 ;;
    --secs)     SECS="${2:?}"; shift 2 ;;
    --label)    LABEL="${2:?}"; shift 2 ;;
    --run-dir)  RUN_DIR="${2:?}"; shift 2 ;;
    --proof-json) PROOF_JSON="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$KIND" ]] || { echo "usage: pcm-gate.sh --kind youtube|hls (--url U | --url-file F) [--secs N] [--label L]" >&2; exit 2; }
[[ "$KIND" == "youtube" || "$KIND" == "hls" ]] || { echo "--kind must be youtube or hls" >&2; exit 2; }
[[ "$SECS" =~ ^[1-9][0-9]*$ ]] || { echo "--secs must be a positive integer" >&2; exit 2; }
[[ -n "$RUN_DIR" && -n "$PROOF_JSON" || -z "$RUN_DIR" && -z "$PROOF_JSON" ]] || {
  echo "--run-dir and --proof-json must be supplied together" >&2; exit 2;
}

PYTHON="${EARSHOT_PYTHON:-python3}"
FFMPEG="${EARSHOT_FFMPEG:-ffmpeg}"
YTDLP="${EARSHOT_YTDLP:-yt-dlp}"
PDT_LOG="${EARSHOT_PDT_LOG:-$REPO/scripts/pdt-log.py}"

if [[ -n "$URLFILE" ]]; then
  [[ -s "$URLFILE" ]] || { echo "PCM-GATE FAIL: url-file missing or empty: $URLFILE" >&2; exit 3; }
  URL="$(head -n1 "$URLFILE")"
fi
[[ -n "$URL" ]] || { echo "PCM-GATE FAIL: no URL (pass --url or --url-file)" >&2; exit 2; }

LABEL="${LABEL:-$KIND}"
EXPECT=$(( SECS * 32000 ))
FLOOR=$(( EXPECT * 80 / 100 ))
ERR="$(mktemp -t pcmgate.XXXXXX)"
trap 'rm -f "$ERR"' EXIT

echo "=== pcm-gate $(date -u +%FT%TZ) label=$LABEL kind=$KIND secs=$SECS ==="
echo "host   : $(hostname -s)  ip-egress-class=$( [[ -f /sys/hypervisor/uuid ]] && echo datacenter || echo local )"
if [[ -n "$PROOF_JSON" ]]; then
  echo "url-sha: $(printf '%s' "$URL" | shasum -a 256 | awk '{print $1}')"
else
  echo "url    : $(printf '%s' "$URL" | cut -c1-120)"
fi
echo "expect : $EXPECT bytes (floor $FLOOR)"

# A durable proof is always one exact schema-3 run/AccessPlan/host. Validate
# before contacting the source so a wrong scheduler context cannot generate
# plausible-looking PCM evidence.
if [[ -n "$PROOF_JSON" ]]; then
  PROOF_EXPORTS=$(EARSHOT_REPO="$REPO" "$PYTHON" - "$REPO" "$RUN_DIR" \
    "$PROOF_JSON" "$URLFILE" "$KIND" <<'PY'
import json, os, shlex, subprocess, sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve()
run = Path(sys.argv[2]); run = (run if run.is_absolute() else repo / run).resolve()
proof = Path(sys.argv[3]); proof = (proof if proof.is_absolute() else repo / proof).resolve()
url_file, kind = sys.argv[4], sys.argv[5]
expected = run / "control" / "pcm-proof.json"
if proof != expected:
    raise SystemExit(f"proof output must be {expected}")
if proof.exists():
    raise SystemExit("PCM proof already exists; an immutable run is proven once")
verify = subprocess.run(
    [sys.executable, str(repo / "scripts/eventctl.py"), "verify", "--run-dir", str(run)],
    cwd=repo, env={**os.environ, "EARSHOT_REPO": str(repo)}, text=True, capture_output=True,
)
if verify.returncode:
    raise SystemExit((verify.stderr or verify.stdout).strip())
access_check = subprocess.run(
    [sys.executable, str(repo / "scripts/access-plan.py"), "validate", "--run-dir", str(run), "--canonical"],
    cwd=repo, env={**os.environ, "EARSHOT_REPO": str(repo)}, text=True, capture_output=True,
)
if access_check.returncode:
    raise SystemExit((access_check.stderr or access_check.stdout).strip())
plan = json.loads((run / "control/run-plan.json").read_text())
access = json.loads((run / "control/access-plan.json").read_text())
host = os.environ.get("EARSHOT_EXECUTION_HOST")
if host != plan.get("execution_host_id") or access.get("capture", {}).get("host_id") != host:
    raise SystemExit("current host does not match RunPlan/AccessPlan")
portable = ""
if url_file:
    path = Path(url_file).expanduser().resolve()
    try:
        rel = path.relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        raise SystemExit("URL file must resolve beneath $HOME")
    portable = "$HOME/" + rel
    if portable not in access.get("endpoint", {}).get("url_files", []):
        raise SystemExit("selected URL file is not declared by AccessPlan")
elif access.get("endpoint", {}).get("url_files"):
    raise SystemExit("AccessPlan declares URL files; one exact --url-file is required")
for key, value in {
    "PROOF_RUN": str(run),
    "PROOF_OUT": str(proof),
    "PROOF_EVENT": plan["event_id"],
    "PROOF_RUN_ID": plan["run_id"],
    "PROOF_PLAN_SHA": plan["plan_sha256"],
    "PROOF_ACCESS_SHA": access["access_plan_sha256"],
    "PROOF_HOST": host,
    "PROOF_URL_FILE": portable,
}.items():
    print(f"{key}={shlex.quote(value)}")
PY
  ) || { echo "PCM-GATE FAIL: RunPlan/AccessPlan proof binding refused" >&2; exit 2; }
  eval "$PROOF_EXPORTS"
fi

publish_proof() { # <continuity-status> <distinct|null> <polls|null> <window|null>
  [[ -n "$PROOF_JSON" ]] || return 0
  local continuity="$1" distinct="$2" polls="$3" window="$4"
  PCM_URL="$URL" PCM_URL_FILE="$PROOF_URL_FILE" PCM_BYTES="$BYTES" \
  PCM_EXPECT="$EXPECT" PCM_FLOOR="$FLOOR" PCM_SECS="$SECS" \
  PCM_KIND="$KIND" PCM_CONTINUITY="$continuity" PCM_DISTINCT="$distinct" \
  PCM_POLLS="$polls" PCM_WINDOW="$window" \
  "$PYTHON" - "$PROOF_OUT" "$PROOF_EVENT" "$PROOF_RUN_ID" \
    "$PROOF_PLAN_SHA" "$PROOF_ACCESS_SHA" "$PROOF_HOST" <<'PY'
import datetime as dt, hashlib, json, os, sys
from pathlib import Path

out, event, run_id, plan_sha, access_sha, host = sys.argv[1:]
url = os.environ["PCM_URL"]
portable = os.environ["PCM_URL_FILE"] or None
file_sha = None
if portable:
    path = Path.home() / portable[len("$HOME/"):]
    if path.is_symlink() or not path.is_file():
        raise SystemExit("URL file stopped being a regular file during PCM proof")
    file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
continuity = {
    "status": os.environ["PCM_CONTINUITY"],
    "distinct_media_sequences": None if os.environ["PCM_DISTINCT"] == "null" else int(os.environ["PCM_DISTINCT"]),
    "polls": None if os.environ["PCM_POLLS"] == "null" else int(os.environ["PCM_POLLS"]),
    "window_seconds": None if os.environ["PCM_WINDOW"] == "null" else int(os.environ["PCM_WINDOW"]),
}
value = {
    "schema": 1,
    "kind": "pcm-capture-proof",
    "created_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "event_id": event,
    "run_id": run_id,
    "run_plan_sha256": plan_sha,
    "access_plan_sha256": access_sha,
    "execution_host_id": host,
    "observed_host": os.uname().nodename.split(".")[0],
    "source_kind": os.environ["PCM_KIND"],
    "endpoint": {
        "url_file": portable,
        "url_file_sha256": file_sha,
        "url_value_sha256": hashlib.sha256(url.encode()).hexdigest(),
    },
    "decoded_pcm": {
        "sample_format": "s16le",
        "sample_rate_hz": 16000,
        "channels": 1,
        "requested_seconds": int(os.environ["PCM_SECS"]),
        "expected_bytes": int(os.environ["PCM_EXPECT"]),
        "floor_bytes": int(os.environ["PCM_FLOOR"]),
        "decoded_bytes": int(os.environ["PCM_BYTES"]),
    },
    "continuity": continuity,
}
canonical = lambda x: json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
value["pcm_proof_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
path = Path(out); tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
try:
    body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    os.write(fd, body); os.fsync(fd)
finally:
    os.close(fd)
os.replace(tmp, path)
dfd = os.open(path.parent, os.O_RDONLY)
try: os.fsync(dfd)
finally: os.close(dfd)
PY
}

# yt-dlp owns YouTube's segment addressing; ffmpeg alone cannot fetch those
# segments even when handed a valid manifest (youtube-resolve.sh:32-34).
if [[ "$KIND" == "youtube" ]]; then
  command -v "$YTDLP" >/dev/null || { echo "PCM-GATE FAIL: yt-dlp not installed" >&2; exit 3; }
  BYTES=$("$YTDLP" -f bestaudio --quiet --no-warnings -o - "$URL" 2>"$ERR" \
          | "$FFMPEG" -loglevel error -i - -vn -f s16le -ac 1 -ar 16000 -t "$SECS" - 2>>"$ERR" \
          | wc -c | tr -d ' ')
else
  BYTES=$("$FFMPEG" -loglevel error -fflags nobuffer -flags low_delay \
            -probesize 200000 -analyzeduration 3000000 -live_start_index -1 \
            -i "$URL" -vn -f s16le -ac 1 -ar 16000 -t "$SECS" - 2>"$ERR" \
          | wc -c | tr -d ' ')
fi
BYTES="${BYTES:-0}"

echo "bytes  : $BYTES"
if [[ "$BYTES" -lt "$FLOOR" ]]; then
  echo "PCM-GATE FAIL: $LABEL yielded $BYTES/$EXPECT bytes (floor $FLOOR) — THIS VANTAGE IS DEAD" >&2
  echo "--- stderr tail ---" >&2
  tail -n 15 "$ERR" >&2
  if grep -qi "confirm you're not a bot\|Sign in to confirm\|HTTP Error 403\|429" "$ERR"; then
    echo ">>> BOT-WALL SIGNATURE DETECTED. This is the HOOD 2026-07-29 failure:" >&2
    echo ">>> the manifest is reachable but segments are refused for this egress IP." >&2
    echo ">>> Capture from a residential vantage, or supply cookies. Do NOT arm here." >&2
  fi
  exit 1
fi

# ── continuity ──────────────────────────────────────────────────────────────
# Bytes prove the URL yielded audio ONCE. They do not prove it will sustain,
# and that distinction cost AAOI-26AUG06: this gate passed twice on 541 KB of
# genuinely decoded audio, and the arm behind it died 32 s later, twice. One
# contiguous read cannot tell a live stream from a stalled one that still has
# a buffer to hand over.
#
# So also require the playlist to be MOVING: poll #EXT-X-MEDIA-SEQUENCE and
# demand it advance. scripts/pdt-log.py already does exactly this (resolves
# master -> variant, emits media_sequence + a computed new_segment per poll),
# so this is a reuse, not a second manifest parser.
#
# HLS only: yt-dlp owns YouTube's segment addressing and there is no playlist
# here to poll. Skipped rather than faked.
if [[ "$KIND" == "youtube" ]]; then
  publish_proof not-applicable-youtube null null null || exit 1
  echo "PCM-GATE PASS: $LABEL yielded $BYTES/$EXPECT bytes of decodable audio on this host"
  echo "  (continuity check skipped — youtube has no pollable media playlist)"
  exit 0
fi

SEQ_SECS="${PCM_GATE_SEQ_SECS:-12}"
SEQ_JSON=$("$PYTHON" "$PDT_LOG" "$URL" 1000 "$SEQ_SECS" 2>"$ERR" || true)
SEQS=$(printf '%s\n' "$SEQ_JSON" | sed -n 's/.*"media_sequence": *\([0-9]*\).*/\1/p' | sort -un | wc -l | tr -d ' ')
POLLS=$(printf '%s\n' "$SEQ_JSON" | grep -c media_sequence || true)

echo "seq    : ${SEQS:-0} distinct MEDIA-SEQUENCE value(s) across ${POLLS:-0} poll(s) in ${SEQ_SECS}s"
if [[ "${SEQS:-0}" -ge 2 ]]; then
  publish_proof advanced "$SEQS" "$POLLS" "$SEQ_SECS" || exit 1
  echo "PCM-GATE PASS: $LABEL decoded $BYTES/$EXPECT bytes AND its playlist is advancing"
  exit 0
fi

if [[ "${POLLS:-0}" -eq 0 ]]; then
  if [[ -n "$PROOF_JSON" ]]; then
    echo "PCM-GATE FAIL: durable capture readiness requires verified HLS continuity" >&2
    tail -n 5 "$ERR" >&2
    exit 1
  fi
  # Could not read a playlist at all. Do NOT fail the arm on this: a venue
  # whose manifest resists polling but whose audio decodes is still armable,
  # and refusing here would trade a real miss for a hypothetical one.
  echo "PCM-GATE PASS: $LABEL yielded $BYTES/$EXPECT bytes of decodable audio on this host"
  echo "  WARN: could not poll MEDIA-SEQUENCE (continuity UNVERIFIED) — stderr tail:" >&2
  tail -n 5 "$ERR" >&2
  exit 0
fi

echo "PCM-GATE FAIL: $LABEL decoded $BYTES bytes but MEDIA-SEQUENCE never advanced" >&2
echo ">>> over ${SEQ_SECS}s / ${POLLS} polls the playlist stood still. This is the" >&2
echo ">>> AAOI-26AUG06 shape: audio decodes from a buffer while the stream behind" >&2
echo ">>> it is already dead, so the arm fires and loses every leg seconds later." >&2
exit 1
