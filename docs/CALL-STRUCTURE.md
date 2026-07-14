# Call-structure spike — does section order predict imminent mentions?

**Status: research spike, offline only.** Nothing here touches the live trading
path (`src/`, `configs/` used at arm time, risk/executor/decision). This note is
the pre-registration + methodology + results for testing one idea: earnings
calls follow a repeatable per-company section order, and knowing which section is
coming next raises the prior that a specific word is imminent.

## Why this is a spike, not a build

Two facts from the codebase reshape the idea (see `docs/ARCHITECTURE.md`,
`VERIFICATIONS.md` §Q4):

1. **The live thesis is a certainty latency-race, not a statistical edge.** The
   system buys YES *only after acoustically hearing the word*, racing Kalshi's
   human video-adjudicators before the strike early-closes. There is no
   probability/fair-value model anywhere — "mispriced" means only "a YES offer
   still rests at or below my cap" (`src/kalshi/executor.rs`).
2. **Detection is binary acoustic keyword-spotting, not transcription.** There is
   no live transcript, LM, or notion of "sections" in the repo today.

So the idea forks into two very different strategies:

- **Path 1 — race amplifier:** section context as a *live prior* that lowers the
  Fast-tier threshold for imminent words, shaving latency. Stays inside the
  certainty thesis and M1/M2 gating.
- **Path 2 — pre-utterance statistical bet:** buy YES on near-certain words
  *before* they're spoken. A new strategy needing a real probability model and
  the M4 gate (`CLAUDE.md`: "Real-size mention trading is gated until M4").

This spike measures whether the premise even holds, then recommends a path.

## Pre-registered questions and thresholds

Declared **before** looking at results (small-n discipline — see Risks).

| # | Question | Metric | Pass if |
|---|----------|--------|---------|
| Q1 | Is per-company section order repeatable? | median normalized edit distance of the `section_type` sequence across a company's quarters | ≤ 0.30 |
| Q2 | Does entering a section predict a keyword? | lift = P(keyword in section S \| call has S) − base rate P(keyword in call) | ≥ 0.25 for ≥1 (section, keyword) pair |
| Q3 | Is there useful lead time? | median seconds from a section's start (its boundary marker) to the first target-keyword occurrence inside it | ≥ 2 s |

Q3 is the make-or-break for **Path 1**: the current spotter floor is ~1 s
(`docs/BAKEOFF.md`), so a lead below that buys nothing. Q2's base rates feed the
**Path 2** feasibility read (a word said in most past calls, priced cheap
pre-call, is the Path-2 signal).

## Data

Two tiers — no live-path data touched:

- **Tier B (depth, timestamped):** the 6 corpus calls in `corpus/raw/`
  (aapl, msft, cat, gis, wmt, qcom), transcribed with **`scripts/transcript-fetch.py`**
  (faster-whisper, same dependency/config as `scripts/corpus-align.py`). This
  yields text **and** word-level timestamps, so a section's char offset converts
  to *seconds* — the only way to measure Q3. Keyword onsets come straight from
  the existing `corpus/manifests/*.toml` ground truth (parsed by
  `src/bench/manifest.rs`); we only detect *section* onsets and subtract.
- **Tier A (breadth, text only):** many quarters of public transcripts per
  company for Q1 order-stability and Q2 base rates. Drop each as
  `corpus/transcripts/<id>.txt` by hand (Motley Fool, IR sites) — these have no
  timestamps and feed Q1/Q2 only. Target ~8+ quarters for aapl/msft/cat/gis/wmt/
  qcom plus JPM (the live-run issuer).

`corpus/transcripts/` is gitignored (raw text stays local); `corpus/structure/`
(the LLM section labels) is committed as small derived analysis.

## Pipeline

All offline, in `scripts/` alongside the existing corpus tooling; none wired into
the Rust binary.

```
transcript-fetch.py   audio (or manual .txt)  ->  corpus/transcripts/<id>.txt
                                                   corpus/transcripts/<id>.words.json
section-label.py       transcript  --LLM-->        corpus/structure/<id>.json
structure-analyze.py   structure + manifest + words -> the three metrics
```

- **`section-label.py`** feeds each transcript to Claude (`claude-opus-4-8`,
  structured output) and gets back an ordered list of labeled sections, each
  anchored by a *verbatim* opening quote (we locate its char offset by exact
  string match in Python — never trust LLM-reported indices) plus the
  `boundary_marker` phrase the model keyed on. The shared section taxonomy is:
  `operator_intro, safe_harbor, ceo_remarks, cfo_remarks, business_segment,
  guidance_outlook, qa, closing`. The marker phrases are the candidate live
  "section markers" a future detector would spot.
- **`structure-analyze.py`** maps section char offsets → ms via the word
  timestamps, assigns each manifest keyword occurrence to its section window, and
  computes Q1/Q2/Q3 against the pre-registered thresholds.

Run:

```
# per call (Tier B): transcribe, then label
.venv-corpus/bin/python scripts/transcript-fetch.py --audio corpus/raw/aapl-q2-2026.mp3 --id AAPL-Q2-2026
.venv-corpus/bin/python scripts/section-label.py --transcript corpus/transcripts/aapl-q2-2026.txt --id AAPL-Q2-2026 > corpus/structure/aapl-q2-2026.json
# once all calls are labeled:
python3 scripts/structure-analyze.py > /tmp/structure-results.json
```

## Results (first pass — 6 calls, rule-based labels)

**Caveats, read first.** This run used `scripts/section-label-rules.py` (a crude
marker-phrase splitter), **not** the LLM labeler — the Anthropic key had no credit
balance. n = 6 calls, **one quarter per company**, so Q1 is uncomputable. Treat
these as a rough feasibility signal, not a verdict.

| Metric | Threshold | Measured | Verdict |
|--------|-----------|----------|---------|
| Q1 order stability (median edit dist) | ≤ 0.30 | **N/A** — no company has ≥2 quarters in the corpus | can't assess |
| Q2 best section→keyword lift | ≥ 0.25 | **0.10** (guidance_outlook → tariff) | **FAIL** |
| Q3 median lead time (s) | ≥ 2 s | **165.9 s** (n=27, spread 0–1411 s) | **PASS** |

Base rates P(keyword in call): guidance **1.0**, tariff 0.5, acquisition 0.5,
partnership 0.17. Because "guidance" is said in *every* call, no section can lift
it — and for the rest, P(keyword | section) barely moves off the call-level base
rate (best lift +0.10). Lead time by section (median): Q&A 302 s, CEO 230 s,
CFO 184 s, guidance 85 s — keywords land well *inside* sections, so there is
plenty of lead time, but it is highly variable (0 to 1411 s), not a fixed offset.

**Read:** there *is* lead time (Q3 passes easily — the necessary condition for
Path 1), but section context does **not** meaningfully predict *which* keyword
above its base rate (Q2 fails — the sufficient condition). On this crude, tiny,
single-quarter data the idea does not clear the bar. The binding weakness is
predictiveness, not timing.

**What would change this read** (do before any go decision): (1) LLM labels
instead of the rule splitter — fuzzy CEO/CFO/guidance boundaries suppress lift;
(2) multiple quarters per company so Q1 is computable and Q2 base rates aren't
dominated by "guidance in every call"; (3) recompute Q2 with a per-*window* rate
(keyword density inside the section) rather than a per-call occurrence flag,
which is coarsely quantized at n=6.

## Risks / guards

- **Tiny n → overfitting.** 6 timestamped calls and a handful of companies is a
  *feasibility signal*, not a validated edge. Report counts; hold ≥2 companies
  out of any pattern-fitting; the thresholds above are pre-registered. Run the
  results past the `quant-audit` skill and the `lookahead-bias-detector` agent
  before trusting any "lift."
- **Lead time may be too short.** If Q3 fails (< ~2 s), Path 1 is dead on arrival
  and only Path 2 (base-rate bet, M4-gated) remains viable — say so plainly
  rather than salvage a weak result.
- **LLM labeling is the weak link.** Hand-check 2–3 `corpus/structure/*.json`
  section boundaries against the raw text before trusting the aggregate.

## Decision gate

**First-pass outcome: not enough to greenlight — but not shelved.** Q3 (lead
time) passes decisively, so the timing premise for Path 1 holds. Q2 (lift) fails:
section context adds little over the keyword's base rate in this data. The failure
is in *predictiveness*, and it is confounded by the three known weaknesses above
(rule-based labels, single quarter per company, coarse metric). So the honest
call is **re-run before deciding**, not "kill":

1. Fund the API key (or `ant auth login`) and re-label with `section-label.py`.
2. Add ≥8 quarters per company of Tier-A transcripts so Q1 becomes computable and
   Q2 base rates diversify. JPM especially (the live-run issuer).
3. Recompute Q2 with a per-window density metric.

Then apply the original gate:
- **All three pass** → propose Path 1 (a section-marker prior in the spotter).
  The lightweight, architecture-native realization is *not* full ASR — it is
  adding the discovered `boundary_marker` phrases as extra keyword-spotter entries
  (`kws_compile` / `streaming_ctc` already support this) plus a contextual
  threshold boost. Run it past the `quant-audit` skill / `lookahead-bias-detector`
  first.
- **Only base rates are strong (Q2 high, Q3 weak)** → Path 2, deferred to M4 with
  a real probability model + quant-audit sign-off.
- **Q2 stays weak after (1)–(3)** → shelve; the strongest interpretation is that
  the target words are too spread across the call for section context to sharpen
  the existing latency-race spotter.
