# Say-Q&A word prior — retail-question traction as a spoken-word predictor

## Mechanism

Say Technologies (app.saytechnologies.com) collects and vote-ranks shareholder questions
BEFORE certain large-cap earnings calls; management answers the top ones on air (Tesla does
this every quarter — IR reads the questions verbatim or near-verbatim). The question list
closes before the call (Tesla Q2-26: 25.5 h before) and is public. Hypothesis: **traction on
Say predicts which words get spoken — including un-obvious words the mention markets haven't
priced**, which today's GEV tape showed is the only pool with edge (7/8 YES words pre-priced
≥83¢; the lone unpriced word was the only machine race — COMPETITOR-ANATOMY §Per-tape GEV).

Prior art: docs/CALL-STRUCTURE.md §Q2 tested section-order as a mention predictor → NEGATIVE.
This is a different, mechanistically stronger signal (the text is literally read aloud).

## Acquisition

The page is a Next.js SPA; questions are client-fetched (verified 2026-07-22: shell HTML,
empty `__N_SSG` pageProps). Current path: drive the operator's Chrome, `get_page_text`, save
to `state/say/<slug>-<date>.txt`. TODO (Part 2): capture the underlying JSON API from network
requests on a fresh load and build `scripts/fetch-say-qa.sh` on the fetch-tape.sh pattern.

## Grading protocol (pre-registered, per event)

BEFORE the call: snapshot Say top questions + tags, snapshot all mention-market prices
(`state/<EVENT>/precall-prices-<date>.json`), and write per-market predictions with
rationale. AFTER settlement: grade. **Headline metric: among markets priced <80¢ (the
unpriced pool), does the Say-signal set settle YES at a rate above (a) its average
price-implied probability and (b) the no-signal unpriced set?** Promotion to
`prepare` integration (src/prepare.rs `tuned_library`/`fill_cap_cents` seam) only after ≥1
event where the signal beats both baselines — and only with operator approval.

---

## Pre-registered: TSLA 2026-07-22 (call 3:30 PM CST; predictions frozen 2026-07-22 ~2:20 PM CST, 70 min pre-call)

Inputs: `state/say/tesla-2026-q2-2026-07-22.txt` (429 questions, top-20 by shares) +
`state/KXEARNINGSMENTIONTSLA-26JUL22/precall-prices-2026-07-22.json` (27 markets, ~2:15 PM).
Tesla historically answers ~4–6 Say questions (institutional + top retail) — acceptance-count
research pending (Part 2), so "top-20 by shares" is the assumed read-pool.

### The test pool — unpriced (<80¢) with a DIRECT Say signal (the words that matter)

| Market | Price (bid/ask) | Say signal | Prediction |
|---|---|---|---|
| MERG Merge/Merger | 39/40 | Q7 (362 v, 672K sh): "offers to ACQUIRE OR MERGE Tesla" — read aloud → word said | **SAID** (high) |
| SCHG Supercharger | 35/36 | Q10 (498K sh, top-10): "Megapods to SUPERCHARGERS" — read aloud → word said | **SAID** (high) |
| ROAD Roadster | 33/35 | Q20 (191K sh, borderline read-pool) + Roadster tag (8 q) | **SAID** (medium) |
| MIA Miami | 72/73 | Indirect: city-expansion questions dominate (Q1/Q3/Q9); answers likely name next cities | **SAID** (medium) |
| SIX Six-Seater | 16/19 | Indirect: Q19 (198K sh) begs the bigger-family-vehicle answer ("six-seat Model Y" is the shipped product) | **SAID** (low-med) |

### Unpriced, NO Say signal (control set — predictions from script/macro prior only)

| Market | Price | Prediction | Note |
|---|---|---|---|
| TARI Tariff | 76/77 | SAID | macro script word (GEV said it); non-Say |
| MEGA Megapack 3 | 74/76 | SAID | energy script; NB Q10 says "Megapods" ≠ Megapack | 
| EURO Europe | 73/74 | SAID | sales discussion; non-Say |
| COMP Competition | 70/72 | UNCERTAIN | generic |
| GROK Grok (3+×) | 63/64 | UNCERTAIN | multi-count bar; AI tag only |
| BERL Berlin | 63/67 | UNCERTAIN | |
| POW Powerwall | 60/62 | SAID | energy script; non-Say |
| AFFO Affordable | 56/57 | SAID | perennial "more affordable models" script |
| INTE Intel | 35/36 | UNCERTAIN | chip-supply rumor; no Say text |
| CYBR Cybertruck | 30/31 | UNCERTAIN | tag 18 but no top-20 question |
| GIGA Gigafactory | 26/27 | UNCERTAIN | generic passing word; market skeptical |
| WAYM Waymo | 22/23 | NOT-SAID | competitor name; execs rarely name it |
| SAFE Safety Monitor | 19/20 | UNCERTAIN | robotaxi-answer adjacent, no direct text |
| TRIL Trillion | 18/19 | UNCERTAIN | comp-plan adjacent (Q7) |
| IRAN Iran/Middle East | 12/13 | NOT-SAID | no signal anywhere |

### Pre-priced (≥85¢ — market already knows; no test value)

ROBO 99, CYBE 99, SPAC 95, SEMI 92, CHIN 91, REGU 86, TARA 85/87. Note TARA (Terafab) is the
one pre-priced word with an EXPLICIT Say driver (Q5, institutional, 1.3M sh) — consistent
with the market pricing Say-visible words; supports the mechanism even though it can't score.

### Scoring (settled 2026-07-22 evening; both events finalized same day)

- **Say-signal set: 1/5 YES (20%)** — SCHG ✓ (froze 36¢ → YES); MERG ✗ (40¢ → NO),
  ROAD ✗ (34¢ → NO), MIA ✗ (72¢ → NO), SIX ✗ (18¢ → NO).
- Baselines: own price-implied ≈ **39.8%**; no-signal <80¢ control set **6/15 = 40%**.
  (Pre-priced ≥85¢ set: 6/7 — the market's one miss was CHIN, 91¢ → NO.)
- **VERDICT (pre-registered metric): FAIL — the Say-signal set underperformed BOTH
  baselines. The promotion gate stays CLOSED.** (n=5; not statistically decisive, but the
  bar was directional and it was missed.)

**Post-mortem (interpretation, not pre-registered):** the upstream mechanism held — the
acceptance model (top-~11-by-shares get read) remains historically solid — but the
question-text → spoken-market-word mapping is lossy: IR paraphrases questions, and a word
appearing in a question's TEXT does not reliably get uttered ("acquire or MERGE Tesla" →
MERG settled NO; even the fast money made this exact mistake — a 27→85¢ machine sweep on
MERG at 21:57 unwound to 7¢ within 5 minutes). SCHG, the one hit, was spoken in
management's own PREPARED remarks, not the question. Any revival of this prior must map
Say traction to the ANSWER-side vocabulary, not question text. Answered-marks for
tesla-2026-q2 were not yet posted at analysis time — re-check in a day to see whether the
merge question was read at all (tests the acceptance model in-sample).

## Acceptance stats — TSLA, last 4 quarters (measured 2026-07-22, PRE-call, from the Say API)

The API marks answered questions post-call (`company_leadership_response` /
`event_host_response`) — a free historical acceptance dataset
(`state/say/tesla-*-hist.json`; fetched via the same endpoint as
`scripts/fetch-say-qa.sh`).

| Event | Questions | Answered | Answered ranks (by shares-upvoted) | in top-10 | in top-20 |
|---|---|---|---|---|---|
| tesla-2026-q1 | 1,078 | 9 | 1,2,3,5,6,7,8,11,450 | 7 | 8 |
| tesla-2025-q4 | 1,504 | 9 | 1,2,3,5,6,7,8,9,11 | 8 | 9 |
| tesla-2025-q3 | 2,007 | 6 | 1,2,4,8,10,11 | 5 | 6 |
| tesla-2025-q2 | 2,306 | 8 | 1,2,3,4,6,8,9,13 | 7 | 8 |

**Findings (answering the operator's two questions):**
1. **How many accepted:** 6–9 per quarter (mean 8).
2. **Which ones:** rank-by-shares-upvoted is a near-perfect selector — 27/32 answered
   questions ranked ≤ 11; 31/32 ranked ≤ 20 (one rank-450 outlier). The read-pool
   assumption in today's pre-registration (top-20 by shares) is historically validated:
   **P(read | rank ≤ 11) ≈ very high; the top ~8 by shares are essentially certain.**

Implication for today's frozen predictions: Q7 (rank 7 — "acquire or merge") and Q10
(rank 10 — "Megapods to Superchargers") fall inside the near-certain read pool → MERG
(39¢) and SCHG (35¢) are the sharpest mispricing candidates if the mechanism transfers.
(Note this section was computed AFTER the 2:20 PM prediction freeze but BEFORE the call —
it sharpens confidence, it did not alter the predictions.)
