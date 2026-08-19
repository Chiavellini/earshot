#!/usr/bin/env bash
# detection-score-day.sh --date YYYY-MM-DD [--host box|local] [--model <faster-whisper id>]
#
# The post-event accuracy pass (detection-accuracy program D0, 2026-08-19): for
# every canonical run of the day that banked audio, transcribe each leg with a
# strong reference (default large-v3-turbo; on the box via ssh unless --host
# local), score EVERY detection + miss per word (scripts/detection-score.py),
# join live verifier SHADOW rows when the report carries them
# (scripts/verify-join.py), and pool the day into
# state/_day/DETECTION-SCORE-<date>.md. Never scores from TTS ratios.
#
# Prereq for --host box: ~/venv-asr with faster-whisper (self-installs if
# missing; ~450 MB) and ~1.6 GB HF cache for the turbo model on first use.
# Box runs must have been pulled first (scripts/opsctl.sh pull --date <D>).
set -euo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; cd "$REPO"
DATE="" HOST="box" MODEL="deepdml/faster-whisper-large-v3-turbo-ct2"
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"; KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
while [[ $# -gt 0 ]]; do case "$1" in
  --date) DATE="$2"; shift 2;; --host) HOST="$2"; shift 2;; --model) MODEL="$2"; shift 2;;
  *) echo "usage: $0 --date YYYY-MM-DD [--host box|local] [--model id]" >&2; exit 2;; esac; done
[[ -n "$DATE" ]] || { echo "need --date" >&2; exit 2; }
STAMP="${DATE//-/}"; STAMP="${STAMP:2}"   # 2026-08-18 -> 260818 (run ids start 2026MMDD)
RUNS=()
for r in var/runs/*/${DATE//-/}T*; do [[ -f "$r/capture/audio.manifest.json" && -f "$r/report/report.json" ]] && RUNS+=("$r"); done
[[ ${#RUNS[@]} -gt 0 ]] || { echo "no canonical runs with banked audio for $DATE under var/runs/ (pull box runs first)"; exit 1; }
echo "runs: ${#RUNS[@]}"
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 "$BOX")
if [[ "$HOST" == "box" ]]; then
  "${SSH[@]}" '[ -x ~/venv-asr/bin/python ] || { python3 -m venv ~/venv-asr && ~/venv-asr/bin/pip install -q faster-whisper numpy; }; mkdir -p ~/transcripts ~/score-runs' 
  scp -q -i "$KEY" scripts/run-transcribe.py "$BOX":~/run-transcribe.py
fi
SCORES=()
for r in "${RUNS[@]}"; do
  E=$(basename "$(dirname "$r")"); R=$(basename "$r")
  OUT="state/$E/transcripts/$R"; mkdir -p "$OUT"
  if ! ls "$OUT"/*.words.json >/dev/null 2>&1; then
    echo "== transcribe $E/$R ($HOST)"
    if [[ "$HOST" == "box" ]]; then
      # ship only the audio + manifest, transcribe there, pull the words back
      rsync -a -e "ssh -i $KEY" --include='*/' --include='capture/audio.manifest.json' --include='capture/wav/*.wav' --exclude='*' "$r" "$BOX":~/score-runs/
      "${SSH[@]}" "~/venv-asr/bin/python ~/run-transcribe.py --run-dir ~/score-runs/$R --out ~/transcripts --threads 8 --model $MODEL" 
      rsync -a -e "ssh -i $KEY" "$BOX":~/transcripts/"$R"/ "$OUT"/
      "${SSH[@]}" "rm -rf ~/score-runs/$R"
    else
      .venv-corpus/bin/python scripts/run-transcribe.py --run-dir "$r" --out "state/$E/transcripts" --threads 6 --model "$MODEL"
    fi
  fi
  python3 scripts/detection-score.py --run-dir "$r" --transcripts "$OUT" --out "state/$E/detection-score" --fuzzy >/dev/null
  SCORES+=("state/$E/detection-score/$R.json")
  if python3 -c "import json,sys; sys.exit(0 if json.load(open('$r/report/report.json')).get('verifier',{}).get('rows') else 1)"; then
    echo "== verifier shadow rows: $E/$R"; python3 scripts/verify-join.py "$r/report/report.json" "state/$E/detection-score/$R.json" | tee "state/$E/detection-score/$R.verify.txt"
  fi
done
python3 scripts/detection-score-pool.py "${SCORES[@]}" --md "state/_day/DETECTION-SCORE-$DATE.md" | tail -15
echo "-> state/_day/DETECTION-SCORE-$DATE.md"
