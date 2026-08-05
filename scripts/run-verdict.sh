#!/usr/bin/env bash
# run-verdict.sh <EVENT_TICKER> [--state-dir DIR] [--no-notify] [--quiet]
# run-verdict.sh --check          # offline self-test over frozen real fixtures
#
# Grade a finished `arm` run: did it actually HEAR anything?
#
# Why this exists (2026-07-29, twice in one day):
#   * WING armed cleanly, ran 14:06→15:09Z, and captured NOTHING.
#     wing-arrival.log was 0 bytes; the fetcher logged spec_404=2095,
#     reserve=0, DEAF-NOW for the whole call.
#   * HOOD armed at 21:00:20Z and both legs were dead 35 s later
#     (ffmpeg exit 183; segs=0 seg_err=71 poll_err=1625), frames_in=0.
# Both wrote a perfectly well-formed report.json and NOTHING OBJECTED. The
# operator was told "HOOD armed successfully" because "the arm started" was the
# only signal anyone checked. `validate-from-log.py` cannot cover this — it says
# so itself: "Exit 0 always; this is a report, not a gate."
#
# So this is the gate. It is deliberately dumb and deliberately loud: audio
# either arrived or it did not, and if it did not, somebody gets paged.
#
# Chosen semantics (operator decision 2026-07-29): FAIL LOUDLY, never
# auto-relaunch. A wrong auto-relaunch mid-call is a worse failure than a
# missed call, and nobody has validated a relaunch path.
#
# ⚠ The load-bearing check is `frames_in == 0`, NOT "every leg died".
# HOOD's real report has `died` set on ONE leg and `died: null` on the other
# (the second leg's decoder ended cleanly with 0 frames, so it never "died"),
# yet the run was totally deaf. An all-legs-died rule would have MISSED IT.
# Verdicts:
#   FAIL  frames_in == 0 / chunks_in == 0 / first_capture_ts == null
#         → nothing was ever heard. This is the WING + HOOD class.
#   FAIL  report.json absent → the arm died before it could write one.
#   WARN  some legs died but audio still flowed. This is NORMAL and by design:
#         the fanout is built to survive sibling loss ("LEG DIED — siblings
#         continue"), and META's real 07-29 run is exactly this shape
#         (421959 frames, 17 detections, spec_edge dead, pipe_tuned alive).
#         Not a failure — but say it out loud, because a silently
#         single-legged run is how Ford bug 5 hid.
#   PASS  audio flowed and every leg survived.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVENT=""; STATE_DIR=""; NOTIFY=1; QUIET=""
CHECK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)     CHECK=1; shift ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --no-notify) NOTIFY=0; shift ;;
    --quiet)     QUIET="--quiet"; shift ;;
    -*) echo "unknown arg: $1" >&2; exit 2 ;;
    *)  EVENT="$1"; shift ;;
  esac
done

page() {  # page <subject> <body>
  [[ "$NOTIFY" == 1 ]] || return 0
  bash "$REPO/scripts/notify.sh" "$1" "$2" $QUIET >/dev/null 2>&1 || true
}

# The whole verdict, as one python filter: stdin=report.json, stdout=one line
# "VERDICT<TAB>headline<TAB>detail", exit 0 always (the caller owns exit codes).
grade() {
  python3 - "$1" <<'PY'
import json, sys

try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"FAIL\treport.json unreadable\t{e}")
    raise SystemExit(0)

frames = d.get("frames_in") or 0
chunks = d.get("chunks_in") or 0
first  = d.get("first_capture_ts")
legs   = d.get("per_leg") or []
dets   = len(d.get("detections") or [])
# INTENTS vs SENT. `orders` lists every intent the executor processed, most of
# which never reach Kalshi (non-crossing, EV gate, dry-run, risk refusal).
# Reporting len(orders) as "orders" said 23 on 2026-08-05 when 0 had been sent.
# Prefer the report's own `sent`; derive it for runs written before that key.
intents = len(d.get("orders") or [])
orders  = d.get("sent")
if orders is None:
    orders = sum(1 for o in (d.get("orders") or []) if o.get("send_ts") is not None)
ev     = d.get("event_id", "?")

dead = [l for l in legs if l.get("died")]
# A leg that reported 0 frames is useless whether or not it set `died`.
mute = [l for l in legs if not (l.get("frames_in") or 0)]

def label(l):
    return l.get("label") or l.get("name") or "?"

legdesc = ", ".join(
    f"{label(l)}={l.get('frames_in') or 0}f" + ("(died)" if l.get("died") else "")
    for l in legs
) or "no per-leg data"

# ── FAIL: nothing was ever heard ─────────────────────────────────────────────
if frames == 0 or chunks == 0 or first is None:
    why = []
    if frames == 0: why.append("frames_in=0")
    if chunks == 0: why.append("chunks_in=0")
    if first is None: why.append("first_capture_ts=null")
    detail = f"{ev}: {', '.join(why)}. legs: {legdesc}."
    for l in dead:
        detail += f" {label(l)} died: {str(l.get('died'))[:160]}"
    print(f"FAIL\tARMED BUT DEAF — captured no audio\t{detail}")
    raise SystemExit(0)

# ── WARN: audio flowed, but not every leg made it ────────────────────────────
if dead or mute:
    lost = ", ".join(label(l) for l in (dead or mute))
    print(f"WARN\taudio OK, {len(dead or mute)}/{len(legs)} leg(s) lost ({lost})\t"
          f"{ev}: frames_in={frames} chunks_in={chunks} detections={dets} "
          f"intents={intents} sent={orders}. legs: {legdesc}. Fanout survived; check the dead leg "
          f"before pooling any latency from this run.")
    raise SystemExit(0)

print(f"PASS\theard audio on every leg\t{ev}: frames_in={frames} "
      f"chunks_in={chunks} detections={dets} intents={intents} sent={orders}. legs: {legdesc}.")
PY
}

# NOTE: deliberately NOT a function whose stdout is captured. An earlier cut
# did `V="$(emit "$LINE")"`, which swallowed the human-readable lines into the
# command substitution — PASS and WARN runs printed absolutely nothing while
# FAIL "worked" only because its lines go to stderr. A reporting tool that is
# silent on success is the exact failure mode this script exists to kill, so
# the verdict is parsed with `cut` and printed inline below.

# ── self-test ────────────────────────────────────────────────────────────────
if (( CHECK )); then
  PASSN=0; FAILN=0
  chk() {  # chk <name> <expected> <actual>
    if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; PASSN=$((PASSN+1))
    else echo "  FAIL  $1 (expected $2, got $3)"; FAILN=$((FAILN+1)); fi
  }
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

  # Fixture 1 — HOOD's REAL 2026-07-29 report, trimmed. Note died on ONE leg
  # only: this is the exact shape an "all legs died" rule would wave through.
  cat > "$TMP/hood.json" <<'J'
{"chunk_ms":80,"chunks_in":0,"detections":[],"event_id":"KXEARNINGSMENTIONHOOD-26JUL17",
 "first_capture_ts":null,"frames_in":0,"lost_races":0,"observe_noncross":0,"orders":[],
 "per_leg":[{"label":"spec_edge","frames_in":0,"chunks_in":0,"detections":0,
             "died":"stream protocol: pipe source idle past 30s — dead webcast stream"},
            {"label":"pipe_tuned","frames_in":0,"chunks_in":0,"detections":0,"died":null}]}
J
  # Fixture 2 — META's REAL 07-29 run: a leg died, audio flowed anyway.
  cat > "$TMP/meta.json" <<'J'
{"chunk_ms":80,"chunks_in":105491,"detections":[1,2,3],"event_id":"KXEARNINGSMENTIONMETA-26JUL29",
 "first_capture_ts":"2026-07-29T20:24:01Z","frames_in":421959,"orders":[],
 "per_leg":[{"label":"spec_edge","frames_in":210897,"chunks_in":52745,"detections":2,
             "died":"stream protocol: pipe source idle past 30s"},
            {"label":"pipe_tuned","frames_in":211062,"chunks_in":52746,"detections":1,"died":null}]}
J
  # Fixture 3 — fully healthy.
  cat > "$TMP/good.json" <<'J'
{"chunk_ms":80,"chunks_in":52745,"detections":[1],"event_id":"KXEARNINGSMENTIONOK-26JUL29",
 "first_capture_ts":"2026-07-29T20:00:00Z","frames_in":210897,"orders":[],
 "per_leg":[{"label":"spec_edge","frames_in":105448,"chunks_in":26372,"detections":1,"died":null},
            {"label":"pipe_tuned","frames_in":105449,"chunks_in":26373,"detections":0,"died":null}]}
J
  # Fixture 4 — frames flowed but first_capture_ts never set (inconsistent report).
  printf '%s' '{"event_id":"X","frames_in":100,"chunks_in":10,"first_capture_ts":null,"per_leg":[]}' \
    > "$TMP/nofirst.json"

  echo "run-verdict self-test:"
  chk "HOOD zero-frames  -> FAIL (died on ONE leg only)" FAIL "$(grade "$TMP/hood.json" | cut -f1)"
  chk "META leg-loss     -> WARN (audio flowed)"         WARN "$(grade "$TMP/meta.json" | cut -f1)"
  chk "healthy run       -> PASS"                        PASS "$(grade "$TMP/good.json" | cut -f1)"
  chk "no first_capture  -> FAIL"                        FAIL "$(grade "$TMP/nofirst.json" | cut -f1)"
  chk "unreadable json   -> FAIL"                        FAIL "$(grade "$TMP/nope.json" | cut -f1)"

  # HOOD's headline must name the deafness, not the dead leg.
  H="$(grade "$TMP/hood.json" | cut -f2)"
  case "$H" in *DEAF*) echo "  PASS  HOOD headline names DEAF"; PASSN=$((PASSN+1));;
               *) echo "  FAIL  HOOD headline was: $H"; FAILN=$((FAILN+1));; esac

  echo
  if (( FAILN )); then echo "SELFTEST FAIL ($FAILN failed, $PASSN passed)"; exit 1; fi
  echo "SELFTEST PASS ($PASSN checks)"; exit 0
fi

# ── real run ─────────────────────────────────────────────────────────────────
[[ -n "$EVENT" ]] || { echo "usage: $0 <EVENT_TICKER> [--state-dir DIR] [--no-notify] | --check" >&2; exit 2; }
cd "$REPO"
DIR="${STATE_DIR:-state/$EVENT}"
REPORT="$DIR/report.json"

if [[ ! -f "$REPORT" ]]; then
  echo "⛔ FAIL — no report at $REPORT" >&2
  echo "   The arm exited without writing a report — it died before or during startup." >&2
  page "$EVENT: NO REPORT" "run-verdict: $REPORT absent after the arm exited. The arm died before writing a report; read the arm log."
  exit 4
fi

LINE="$(grade "$REPORT")"
V="$(cut -f1 <<<"$LINE")"; H="$(cut -f2 <<<"$LINE")"; D="$(cut -f3 <<<"$LINE")"

case "$V" in
  PASS)
    echo "✅ PASS — $H"; echo "   $D"
    exit 0 ;;
  WARN)
    echo "⚠️  WARN — $H"; echo "   $D"
    page "$EVENT: leg lost (audio OK)" "$H
$D"
    exit 0 ;;
  FAIL)
    echo "⛔ FAIL — $H" >&2; echo "   $D" >&2
    page "$EVENT CAPTURED NOTHING" "$H
$D
report: $REPO/$REPORT"
    exit 3 ;;
  *)
    echo "⛔ FAIL — verdict unparseable: $LINE" >&2
    exit 5 ;;
esac
