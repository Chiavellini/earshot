# Prefix-safety: which words can we fire early on

The two-tier detector can fire a **Fast** tranche on a partial word (a
`fast_prefixes` entry) before the speaker finishes it, then release the rest on
the full-word **Confirmed** hit (`src/decision.rs`, `FAST_TRANCHE_PCT`). This is
the "fire before the word completes" lever. The gain is only real on a
low-lookahead engine (`streaming_ctc`); on `sherpa_kws` a token-early hit saves
~35–70 ms because the model bakes in ~320 ms of lookahead (`docs/BAKEOFF.md`).

The question "which words are safe to fire early on" is **not** an orthographic
one ("inflati…" looks unambiguous → inflation). It is decided by the model's BPE
**token boundaries** and by acoustic collisions with other spoken words. A
candidate `fast_prefix` must pass three gates, in order.

## The tool

`earshot compile-keywords --config <cfg> [--model-dir <dir>]` prints, per keyword,
the Confirmed token sequence per variant and — for each `fast_prefixes` candidate
— the cut tokens or why it is rejected. It reads the model's `bpe.model`; pass
`--model-dir` to analyze a different model's tokenizer. This implements Gate (a)
and is model-parameterized, so it serves any engine (sherpa or CTC).

## Gate (a) — token boundary

A Fast prefix is the full word's token sequence **cut at a token boundary**, never
an independently re-encoded prefix (`src/detector/kws_compile.rs`: `compile`,
`prefix_cut`; the `artificial intel` trap test). The cut must be strictly shorter
than the full word, else the prefix is useless (fires no earlier). Rejections:
`REJECTED — not shorter than full` (no boundary before the last token) or
`REJECTED — no variant starts with this prefix` (typo / wrong variant).

Worked results on the **gigaspeech BPE** (`compile-keywords` output):

| word | full tokens | shortest usable cut | boundary verdict |
|------|-------------|---------------------|------------------|
| tariff | `▁TA RI FF` | `▁TA RI` ("tari") | ✓ cut 2/3 |
| recession | `▁RE CE S S ION` | `▁RE CE S S` ("recess") | ✓ cut 4/5 |
| acquisition | `▁A C QU IS ITION` | `▁A C QU IS` ("acquis") | ✓ cut 4/5 |
| guidance | `▁GU ID ANCE` | — | ✗ only `▁GU ID` ("guid"), see Gate (b) |
| partnership | `▁PART N ER SHIP` | — | ✗ `▁PART` = the word "part"; no safe boundary |
| inflation | `▁IN F L ATION` | `▁IN F L` ("infl") | ✗ acoustically unsafe, see Gate (b) |

Note inflation: the BPE lumps "ation" into one token, so the ONLY cut shorter than
the full word is at `▁IN F L` ("infl") — much earlier than the orthographic
"inflati…" one might expect, and busy (Gate b).

## Gate (b) — acoustic non-collision

The cut token subsequence must not also prefix common call-English, or it fires on
the wrong word. Method: the **FA autopsy** — bench a dev call + a false-alarm
control call, read the `fa_autopsy` entries in `bench-report.json`
(`src/bench/mod.rs`; format `phrase tier call@posMs`), and independently
re-transcribe those positions. If a phonetic neighbor fires, lengthen the prefix
by one token and repeat.

Worked precedents:
- **recession**: `reces` (`▁RE CE S`) collided with *reception / session* on
  telephony audio → lengthened to `recess` (`▁RE CE S S`), FA 6.05→1.78/h
  (`configs/bakeoff.toml` comments, `docs/BAKEOFF.md`).
- **guidance** `▁GU ID` ("guid") also prefixes *guiding / guided* (said
  constantly) — structurally unsafe, dropped.
- **inflation** `▁IN F L` ("infl") prefixes *influence / influx / inflate /
  inflection* — unsafe. inflation has no safe Fast prefix on this tokenizer.

## Gate (c) — market-outcome safety

Kalshi Mentions score plurals and possessives as the **same** mention, with no
stemming (`VERIFICATIONS.md` §Q4). So a singular-word prefix is market-safe iff all
its completions still contain the mention word. For tariff/recession/inflation the
singular and plural map to one ticker, so singular↔plural is market-irrelevant.
The trap is a prefix that could complete to an *off-market* word — which is
exactly Gate (b). Also re-check per-market `rules_primary`: a "said N times" or
multi-word strike changes the one-shot firing assumption.

## Model-specificity (do not skip)

Every cut above is specific to the **gigaspeech BPE**. A different model
(e.g. a new English streaming-CTC export) has its own tokenizer and its own
boundaries — `tari`/`recess`/`infl` do **not** transfer. Re-run a → b → c with
`compile-keywords --model-dir <new-model>` and a fresh FA autopsy on that model.
The `streaming_ctc` engine maps pieces to its own `tokens.txt`, so an unmapped
piece is a clean build error (`token piece … not in tokens.txt`), not a silent
mismatch.
