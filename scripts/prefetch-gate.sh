#!/bin/bash
# PRE-REGISTERED DROP-SAFETY GATE for `hls-edge-fetch.py --prefetch`.
#
# Bar, identical to the one the speculative posture had to pass (LATENCY-PROOF
# S7): **ZERO audio deficit vs the plain manifest-driven leg over 90 minutes.**
#
# Why 90 minutes and not 150 seconds. The speculative posture passed every short
# run with seg_err=0 and then silently lost ~86 s of audio across a full call, as
# ~14-17 gaps of ~6 s scattered through it — any one of which can swallow a
# keyword. It is a recall bug wearing a latency bug's clothes, and only a
# full-length run surfaces it.
#
# Both legs are pointed at the MASTER url so each resolves its own media
# playlist: IVS media URLs are signed tokens and one expiring mid-run would
# otherwise kill the comparison.
set -u
DUR=${1:-5400}
# Extra flags for the candidate leg. Default is the buffered tier; pass
# "--prefetch --stream-body" to gate the streamed one (measured +2.073 s
# first-byte lead, n=600 — but a 20-min run is not the 90-min bar).
CAND_FLAGS=${2:---prefetch --prefetch-report}
MASTER="https://fcc3ddae59ed.us-west-2.playback.live-video.net/api/video/v1/us-west-2.893648527354.channel.DmumNckWFTqz.m3u8"
OUT=$HOME/pfgate
mkdir -p "$OUT"
cd "$HOME/ear" || exit 1

# ⚠ NOT a function returning $! through $( ). Command substitution waits for
# every process holding the pipe — INCLUDING a background job that inherited it —
# so `P=$(leg prefetch …)` blocks for the whole run and the second leg never
# starts. That produced a one-legged "gate" with nothing to compare against on
# the first attempt. Both legs are launched inline, stdout to /dev/null.
FF="-loglevel warning -fflags +discardcorrupt+nobuffer -flags low_delay -probesize 200000 -analyzeduration 3000000"

echo "=== $(date -u) GATE START  dur=${DUR}s  cand_flags=[$CAND_FLAGS]  master=IVS demo"
( python3 -u scripts/hls-edge-fetch.py "$MASTER" 250 2 $CAND_FLAGS \
    2>"$OUT/prefetch.err" \
  | ffmpeg $FF -i - -vn -ac 1 -ar 16000 -t "$DUR" -y "$OUT/prefetch.wav" \
    2>"$OUT/prefetch.ffmpeg.err" ) >/dev/null 2>&1 &
P=$!
sleep 2
( python3 -u scripts/hls-edge-fetch.py "$MASTER" 250 2 \
    2>"$OUT/plain.err" \
  | ffmpeg $FF -i - -vn -ac 1 -ar 16000 -t "$DUR" -y "$OUT/plain.wav" \
    2>"$OUT/plain.ffmpeg.err" ) >/dev/null 2>&1 &
Q=$!
echo "pids: prefetch=$P plain=$Q"

# WALL-ANCHORED sampler — the verdict depends on it. See prefetch-verdict.py for
# why final WAV durations cannot decide this gate.
( while kill -0 "$P" 2>/dev/null && kill -0 "$Q" 2>/dev/null; do
    a=$(stat -c %s "$OUT/prefetch.wav" 2>/dev/null || echo 0)
    b=$(stat -c %s "$OUT/plain.wav" 2>/dev/null || echo 0)
    echo "$(date -u +%H:%M:%S) prefetch=$a plain=$b delta=$((a-b))" >> "$OUT/samples.log"
    sleep 30
  done ) >/dev/null 2>&1 &
S=$!

wait $P $Q
kill "$S" 2>/dev/null
echo "=== $(date -u) capture done"

for l in prefetch plain; do
  d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/$l.wav" 2>/dev/null)
  echo "$l duration=${d}s  $(grep -c 'seg LOST' "$OUT/$l.err" 2>/dev/null) lost, $(grep -c 'short read' "$OUT/$l.err" 2>/dev/null) short, $(grep -c 'catch-up' "$OUT/$l.err" 2>/dev/null) catchup"
  tail -1 "$OUT/$l.err" | grep SUMMARY || grep SUMMARY "$OUT/$l.err" | tail -1
done

python3 "$HOME/ear/scripts/prefetch-verdict.py" "$OUT/samples.log"

echo "=== $(date -u) PREFETCH-GATE-DONE"
