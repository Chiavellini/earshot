---
name: detector-improve
description: Strengthen detection at the model/keyword level using a finished event's banked audio. Transcribes the tape, scores precision and recall against it, autopsies every miss into fixable buckets, banks the audio into the bake-off corpus, widens variants and retunes per-keyword thresholds from measured evidence, re-benches with interleaved A/B, and propagates through the study→posture→preflight interlock. Use after an event ends when the user asks to score the event, improve detection, study misses or false fires, or asks why a word was missed. Analysis and keyword/threshold changes only — it never touches risk caps, posture floors, or pre-registered acceptance bars.
---

# Detector improve — turn one event's tape into measured detection gains

## Purpose

Consume a finished event's banked audio + detection record and produce: a
scored precision/recall verdict, every miss and false fire bucketed into its
fix class, the audio banked into the corpus, keyword variants/thresholds
updated on measured evidence, an interleaved A/B bench verdict, and the
study→posture→preflight chain re-run so the NEXT event benefits. Model-level
means: variants, per-keyword thresholds, engine/precision choice, corpus
growth — the levers that exist in-repo. New model exports are a G-ladder
question, not this skill.

## The incident this exists to prevent

2026-08-11: the system filled $48.50 across 5 orders and banked **no audio at
all** — "We bought positions we cannot audit." And when audio does exist, naive
scoring lies: detection times are relative to `first_capture_ts`, transcript
times to WAV start; the origins differed by 58.8 s on HIMS (naive scoring
graded a 9/9 run as 7/9, calling two TRUE positives false) and 2208.6 s on
COHR. Every verdict in this skill flows through alignment-corrected scoring or
is labeled unproven.

## Scope guard — run this before anything else

1. **Pull first** (defect E3: `deploy-box.sh` excludes `state/` both ways — a
   box run is invisible to every grader until pulled):
   ```sh
   scripts/opsctl.sh pull --event <EVENT_ID>      # or --date YYYY-MM-DD
   ```
2. Assert the tape exists: `ls -la state/<EVENT_ID>/capture/call.wav` (or the
   canonical `var/runs/<E>/<R>/capture/` once E1 closes — say which you read).
3. **No audio ⇒ stop honestly.** Every miss on an audioless run is
   `NOT_RESOLVABLE(no-audio)` — never "detector miss". Report that and the E-lane
   dependency; do not improvise verdicts from the market tape.
4. `git status --short` / `git log --oneline -3` — nightly automation and the
   reliability lanes commit mid-session; never sweep their files into yours.

## Phase 1 — TRANSCRIBE (glue for gap: no wrapper exists)

`transcript-fetch.py` hardcodes its output to `corpus/transcripts/`; the
state-side copies are glue this skill owns (COHR-verified chain):

```sh
# one-time if absent: python3 -m venv .venv-corpus && .venv-corpus/bin/pip install faster-whisper
.venv-corpus/bin/python scripts/transcript-fetch.py \
  --audio state/<E>/capture/call.wav --id <E>
cp corpus/transcripts/<e-lowercase>.txt        state/<E>/capture/transcript.txt
cp corpus/transcripts/<e-lowercase>.words.json state/<E>/capture/transcript.words.json
```

(`faster-whisper small.en`, int8, word timestamps; the HF cache is offline-
capable for `small.en`.)

## Phase 2 — SCORE

```sh
.venv-corpus/bin/python scripts/score-precision.py --event <E> \
  --audio state/<E>/capture/call.wav \
  --save-transcript state/<E>/capture/transcript-segments.json \
  | tee state/<E>/capture/precision-score.txt
```

- Exit 3 = alignment not established — the refusal text says why (too few
  intents, disjoint clusters); do NOT hand-pick an offset.
- A printed residual-spread warning means every verdict below it is unproven —
  say so in the report.
- Presence-only mode (`.txt` transcript) prints `SAID`, never `TRUE POS` — the
  two are never averaged.
- It reads the report box-first and counts wire orders correctly
  (`send_ts`/`executed`); "OF THE N THAT REACHED THE WIRE: X said, Y never
  said" is the false-fire headline.

Then join settlement truth (leave-one-event-out by construction — quote its
numbers freely):

```sh
python3 scripts/settle-score.py --event <E>
python3 scripts/settle-score.py --keywords     # per-keyword precision, all banked events
python3 scripts/settle-score.py --bands        # P(YES | fired, ask) by price band
```

## Phase 3 — AUTOPSY every miss and false fire

For dead words from the event's study (the working instrument — controls are
mandatory, and if a control does not FIRE every verdict below is void):

```sh
source ~/.kalshi/earshot_env   # ORT_DYLIB_PATH — streaming_ctc hangs silently without it
scripts/dead-word-autopsy.sh --auto --study-dir /tmp/variant-study-<config-basename>
```

For LIVE misses (word said on tape, no detection) — cut the clip inline (gap:
no clip-cutting tool exists) and force-explain it at the armed operating point:

```sh
# onset from transcript.words.json; detect-explain requires 16 kHz mono
ffmpeg -ss <onset_s - 2> -t 4 -i state/<E>/capture/call.wav \
  -ac 1 -ar 16000 -c:a pcm_s16le /tmp/miss-<word>.wav
target/release/earshot detect-explain --config configs/<event-cfg>.toml \
  --wav /tmp/miss-<word>.wav --model-dir models/<the CTC model dir> [--phrase <word>]
```

⚠ THE MODEL-RESOLUTION TRAP: armed configs deliberately keep a
`[detector.sherpa_kws]` block, and a bare `compile-keywords`/`detect-explain`
resolves the FIRST block — the GigaSpeech vocab, not the CTC one.
`--model-dir` is mandatory.

Bucket every miss — the fix differs per bucket:

| bucket | meaning | fix |
|---|---|---|
| NEAR-MISS | peak confidence just under threshold | per-keyword `confirmed_threshold` from the measured margin; widening cannot help |
| NO-EVIDENCE | a BPE piece the model never emits | spelling/variant fix (Phase 5); **unreachable by any threshold** |
| false fire | fired on a word never said | check collision variants; k≤2 tokens; report to posture |

(`miss-autopsy.py` increment [2] — forced-decode scoring — is a TODO in-tree;
only `--signature` works. Use `dead-word-autopsy.sh` + `detect-explain`.)

## Phase 4 — BANK the event into the corpus

```sh
scripts/corpus-process.sh --wideband corpus/raw/<id>.<ext>   # webcast channel: 16 kHz, NO band-limit
python3 scripts/manifest-from-words.py --id <id> --split dev \
  --keywords '[{"phrase":"...","variants":["..."]}]'
python3 scripts/corpus-verify.py corpus/manifests/<id>.toml --sample 0.2   # by ear, macOS
```

- **Never telephony-degrade a webcast leg** — bench each engine on the channel
  it trades on.
- `split` is pre-committed at creation (dev vs holdout) and never flipped.
- ⚠ THE `.toml.pending` RULE: a manifest whose audio is not banked **hard-fails
  every bench run on the machine** — name it `<id>.toml.pending` until the
  audio lands; renaming to `.toml` is step 0 of the fill.

## Phase 5 — WIDEN / RETUNE (measured evidence only)

```sh
python3 scripts/variant-widen.py --config configs/<cfg>.toml --only <SUF,SUF>          # proposal
python3 scripts/variant-widen.py --config configs/<cfg>.toml --only <SUF,SUF> --write  # adopt: invalidates keywords_sha256
```

- Widen **transcriptions of the phrase, never synonyms for the concept** (the
  CELH `nfl` rule: the market resolves on the letters being SAID).
- `MAX_VARIANTS = 8` is a study-runtime budget.
- Lexicon entries (`configs/variant-lexicon.toml`) are admitted only with a
  measured evidence string (study confirmed/clips or a live fill) — "nothing
  goes in here on the strength of an argument." Mark recall-for-precision
  trades `collision = true` and proven-dead sets `dead = true` so nobody
  re-derives them.
- NEAR-MISS thresholds: set per-keyword `confirmed_threshold` in the config
  from the measured margin — a config-bytes change (preflight verdict voids;
  the study stays valid because thresholds are outside `keywords_sha256`).
- A widened variant that resolves the WRONG market is the one risk widening
  cannot see — re-run `scripts/market-phrase-screen.py` against the live board.

## Phase 6 — RE-BENCH (interleaved, or it is noise)

```sh
source ~/.kalshi/earshot_env
cargo run --release -- bench-detector \
  --config configs/<bench-cfg>.toml --corpus corpus/ \
  --engines streaming_ctc --chunk-ms 160 \
  --model-dir <baselineDir>,<candidateDir> \
  --split dev --out /tmp/bench-<what>.json
```

- **All compared cells in ONE invocation** — the laptop has a documented 3.5×
  wall-clock run variance; separate runs cannot be compared.
- Optimize `emit_lag`/`consumed_lag_p50_ms` (audio-domain, clock-free);
  cross-engine comparisons read `consumed_lag`, never `emit_lag`.
- The pre-registered bars are quoted in `references/MODEL-VERDICTS.md`; they
  are **not negotiable after results are seen**. `holdout` runs ONCE with a
  frozen config; dev is for tuning.
- The standing feedback rule (2026-07-17): a detection/latency task ends with
  interleaved A/B or side-by-side numbers, or an explicit **not-resolvable
  verdict** — never a narrative improvement claim.
- First check `references/MODEL-VERDICTS.md` — P1/P3/G2b are ANSWERED; do not
  re-run closed questions.

## Phase 7 — PROPAGATE through the interlock

Any keyword change (Phase 5) voided the study. Run the chain end-to-end or the
next event silently refuses at T-75 (the TTWO lesson: 12 markets / 69,658
contracts forfeited protecting 3 provably-dead words):

```sh
scripts/variant-study.sh configs/<cfg>.toml                 # macOS only; artifact written even on exit 1
python3 scripts/posture-from-study.py configs/<cfg>.toml \
  --compile-with state/<EVENT_ID>/earshot-pinned --model-dir models/<dir>
scripts/preflight-event.sh --config configs/<cfg>.toml --only-readiness   # isolate the gate
scripts/assert-ready.sh configs/<cfg>.toml
```

`study-nightly.sh` clones a fresh artifact to every same-`keywords_sha256`
sibling automatically — a widened set propagates for free.

## Phase 8 — REPORT

Per-word before/after table (study ratio, live verdict, bucket, action taken),
lexicon diff, corpus delta (new manifests + split), bench verdict with the
exact cells, and an explicit list of what remains dead with its evidence.
Journal it: `scripts/journal.sh --event <E> --actor detector-improve --action
grade --outcome ok --detail "precision X/Y; widened N; thresholds M" || true`.

## What this skill refuses to do

1. **Never moves a pre-registered bar** and never re-runs a holdout to get a
   better number — holdout is a once-only governance event.
2. **Never touches posture floors, caps, bands, or `src/risk.rs`** — posture
   is re-derived only via `posture-from-study.py`; money policy is the
   operator's (SYSTEM-STATE §4).
3. **Never claims an improvement without interleaved A/B numbers** — "looks
   better" is not a verdict; not-resolvable is.
4. **Never scores against an unaligned transcript** — exit 3 means stop, and a
   large residual spread means every verdict is labeled unproven.
5. **Never adds synonym variants** — transcriptions only.
6. **Never re-answers closed questions** (`references/MODEL-VERDICTS.md`).
7. **Never edits `scripts/` or `src/`** — a fix that needs code goes to the
   repo lanes as a proposal with evidence.

## Reference

- `references/MODEL-VERDICTS.md` — the answered-questions ledger (P1, P3,
  fp32, G2b, correction tags) so no session re-litigates them.
- `docs/BAKEOFF.md` — the full bench protocol and bar tables.
- `docs/SYSTEM-STATE.md` — defect IDs to cite when blocked (E1/E3 evidence
  gaps, M3 precision-into-grade).
