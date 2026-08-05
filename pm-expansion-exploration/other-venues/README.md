# Task 2 — wider mention-market venue survey

Beyond earnings calls: does the same audio→detection pipeline work on other
spoken-word prediction markets? The core architectural fact that motivates
this track: the audio-ingestion-through-detection half of EARSHOT
(`SourceConfig::Pipe`/`Hls` → detector) is already venue-agnostic and
proven live on a non-financial source (`configs/webcast-abcwnt-capture.toml`,
ABC World News Tonight via YouTube). Execution (order placement) is
Kalshi-only and would need real new code for any Polymarket-only venue —
see `../profitability/` for that finding in detail. This track is about
audio/market discovery and feasibility, not execution.

## What's here

- `candidate-longlist.md` — Pass A: mined `slate/census.json`'s 316
  unclassified `MENTION` tickers (not the 5 already named in
  `docs/EXPANSION.md`) into 7 thematic clusters.
- `pass-b-kalshi-widescan.md` — Pass B: live sweep of Kalshi categories
  beyond `category=Mentions`, looking for mention-style markets the
  existing `mentions-census.py` sweep structurally can't see.
- `pass-c-pm-widescan.md` — Pass C: live sweep of Polymarket's Gamma
  catalog beyond the single `tag_slug=mention-markets` pull.
- `venue-survey.json`/`.md` — Pass D: checklist depth (market state, stream
  source, cadence, speech characteristics, blockers) on the top candidates
  from clusters A (government remarks) and C (podcasts).
- `candidate-scoring.md` — hard-gate + weighted score, sanity-checked
  against `docs/EXPANSION.md`'s own existing verdicts (WNT/sports PARKED,
  FOMC "the crown") before trusting it on anything new.
- `prototype-configs/` — illustrative-only capture sketches, never
  registered in `configs/`, never armed.

## Headline finding so far

**TBPN** (Technology Business Programming Network, `KXTBPNMENTION`) scores
highest of anything surveyed: a fixed daily live window (weekdays 11am-2pm
PT), a single confirmed official YouTube channel, ~250 episodes/yr, and no
hard-gate blocker. Its Kalshi board is currently dormant (0 open events),
so it's a "watch for the next board" candidate, not immediately armable.

**Mamdani (NYC Mayor)** has a real, live board right now
(`KXMAMDANIMENTION-26AUG05`, 14 words) — but it resolves on his *next*
unscheduled announcement, structurally the same ad-hoc-discovery shape
already flagged for the Trump-remarks class in `docs/EXPANSION.md`.

**Government-official speeches generally** (Fed governors beyond Powell,
cabinet officials) are real, blocker-free, and confirmed to get
livestreamed — but each event's actual video lives on a different
third-party host's channel, making per-event discovery the binding cost
rather than any technical blocker.

See `candidate-scoring.md` for the full comparison table and
`venue-survey.md` for the detail behind each score.
