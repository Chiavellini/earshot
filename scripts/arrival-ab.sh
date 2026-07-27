#!/bin/bash
# Three-way arrival A/B on one live IVS channel. Answers, in one run:
#   plain      manifest-driven          -> the baseline
#   prefetch   --prefetch (buffered)    -> is the modelled ~0.92 s real?
#   pfstream   --prefetch --stream-body -> does streaming add the rest, SERIALLY?
#
# The third leg is the cheap version of the "streamed tier". The concurrency
# design assumed a fetch occupies ~3.3 s against a ~2 s URL cadence, but the
# fetch RETURNS at 3.3 s and by then the next prefetch URL is already ~1.3 s into
# its production window — so the loop should self-correct to continuous
# streaming without any concurrency. Worth testing before writing threads.
#
# stdout goes to /dev/null on purpose: this measures ARRIVAL, and removing ffmpeg
# removes it as a confound. Production backpressure is a separate question.
set -u
DUR=${1:-1200}
MASTER="https://fcc3ddae59ed.us-west-2.playback.live-video.net/api/video/v1/us-west-2.893648527354.channel.DmumNckWFTqz.m3u8"
OUT=$HOME/arrab
rm -rf "$OUT"; mkdir -p "$OUT"
cd "$HOME/ear" || exit 1

echo "=== $(date -u) ARRIVAL-AB START dur=${DUR}s"
python3 -u scripts/hls-edge-fetch.py "$MASTER" 250 2 \
  --arrival-log "$OUT/plain.arr" >/dev/null 2>"$OUT/plain.err" &
A=$!
python3 -u scripts/hls-edge-fetch.py "$MASTER" 250 2 --prefetch \
  --arrival-log "$OUT/prefetch.arr" >/dev/null 2>"$OUT/prefetch.err" &
B=$!
python3 -u scripts/hls-edge-fetch.py "$MASTER" 250 2 --prefetch --stream-body \
  --arrival-log "$OUT/pfstream.arr" >/dev/null 2>"$OUT/pfstream.err" &
C=$!
echo "pids plain=$A prefetch=$B pfstream=$C"

sleep "$DUR"
for p in $A $B $C; do kill "$p" 2>/dev/null; done
sleep 2
echo "=== $(date -u) capture done"

for l in plain prefetch pfstream; do
  echo "-- $l: $(wc -l < "$OUT/$l.arr") segments; $(grep -c 'seg LOST' "$OUT/$l.err") lost, $(grep -c 'short read' "$OUT/$l.err") short"
done

python3 scripts/arrival-lead.py "$OUT/plain.arr" "$OUT/prefetch.arr" "$OUT/pfstream.arr"
echo "=== $(date -u) ARRIVAL-AB-DONE"
