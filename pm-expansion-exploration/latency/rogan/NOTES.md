# Rogan pack-lag: Polymarket trades-depth verification

**Question**: does `data-api.polymarket.com/trades` support the same
free, no-auth, purely post-hoc pull that makes the `wnt-pack-lag.py`
pattern need zero live capture — or does it only serve a recent/live
window, forcing a scheduled live capture via `scripts/pm-tape.py` instead?

## Method

Pulled `GET https://data-api.polymarket.com/trades?market=<conditionId>&limit=500`
(no auth, same call shape as `scripts/pm-capture.py:44,119`) against two
real markets:

1. **A currently-open Rogan market** (this week's episode, found via
   `GET gamma-api.polymarket.com/events?closed=false&tag_slug=mention-markets`):
   `Will "People" be said 200+ times during the first Joe Rogan Experience
   of the week of August 3?` — only 2 trades so far (market opened
   2026-07-31, still thin). Not useful for depth-testing, just confirms the
   live path works.
2. **A settled, ~22-month-old market** (found via `public-search?q=joe
   rogan say`): `Will Trump say "trans" on The Joe Rogan Experience?`
   (event `what-will-trump-say-during-rogan-podcast`, closed, $1.15M event
   volume, `endDate: 2024-10-25T12:00:00Z`). Pulled its trades with the
   same call, `limit=500`.

## Result

**482 trades returned**, timestamps spanning **2024-10-23 18:15:41Z →
2024-10-26 06:03:19Z** — the full life of the market (opens before the
podcast, resolves shortly after), fetched fresh today (2026-08-05), **~22
months after the fact**. No truncation observed (482 < the 500 limit
requested). This is a definitive, verified answer, not an inference.

## Verdict

**PM's trades endpoint behaves exactly like Kalshi's for this purpose.**
The Rogan pack-lag check does **NOT** need a scheduled live capture via
`pm-tape.py` — it can use the identical free/post-hoc pattern
`wnt-pack-lag.py` uses against Kalshi: pick any settled Rogan-adjacent PM
market, pull its full trade history any time after the fact, join it
against a leg-1 timing reference (a same-day YouTube VOD transcript, same
shape as the TBPN build). This removes the cost increase the plan
flagged as a real risk — it turned out not to apply.

**Caveat, not fully closed out**: only tested on one settled market found
via search (a 2024 Trump/Rogan crossover event, not a "pure" weekly Rogan
mention board like the currently-open one above). The regular weekly
`KXROGANMENTION`-style Polymarket board is new (started appearing
~2026-07-31 per the open-events pull) and has no settled instance yet to
test depth against directly — the verdict above rests on PM's trades
endpoint behaving consistently across market types (a reasonable
assumption, since it's a generic venue-wide endpoint, not market-specific
plumbing), not on a same-shape settled market. Worth a second confirmation
once the first weekly Rogan board actually settles.

**Not done here** (out of scope for this pass): the actual leg-1
VOD-transcript build for Rogan, or running the join. This note only
answers the trades-depth question that gates which pattern to use.

---

## Update 2026-08-05 (later same day) — leg-1 built, join run, real findings

**Market re-selection.** The Trump/"trans" crossover market above was NOT
used for the actual join — it requires speaker attribution (only Trump's
words count) that YouTube auto-captions can't give. Found a much cleaner
market shape instead: "What will be said on the first Joe Rogan Experience
episode of the week? (July 27)" (Polymarket, closed 2026-08-02) — every
strike resolves on the word being said "by anyone... regardless of
context," exactly matching WNT/TBPN's shape. Episode identified precisely:
JRE #2531 (Forrest Galante, published 2026-07-28T17:00:07Z, confirmed via
yt-dlp's second-precision `timestamp` field — the day-only `upload_date`
field is NOT precise enough for this measurement and was explicitly
rejected as an anchor, see `pack_lag_rogan.py`).

**Built**: `pm_sweep.py` (PM-side counterpart to `lib/kalshi_sweep.py`,
self-tested), `pack_lag_rogan.py` (leg-1 word-match + join, self-tested),
transcript banked at `corpus/20260728/` (35,594 words, 10,541s episode).

**Real run result: all 3 single-mention strikes with real volume (SPY,
ALIEN, CRAZY) came back "pre-priced"** — but the raw trade timelines
revealed something more useful than a clean offset would have been:

- **CRAZY** repriced to ~0.93 a full DAY before the episode even published
  — base-rate pricing (said 79x in this one episode alone), not a reaction
  to this episode's content. Same phenomenon as MLB's GRAND SLAM/WALK OFF
  finding.
- **ALIEN** jumped 0.87→0.99 within 2 minutes of publish. Root cause
  found: the video's own description text reads "...his new special,
  'Alien Sharks: Untamed America,' premieres July 29..." — the word is
  sitting in plain text at t=0, no audio needed. **A real, previously-
  unknown fast channel for this venue class**: something (bot or human) is
  scanning video descriptions at publish and beating any conceivable
  audio-based detector for words that leak into show notes.
- **SPY** also jumped to ~0.99 within 2 minutes of publish, but "spy"
  appears in none of the description/tags/chapters, and its actual first
  utterance in the transcript is 81+ minutes in. **Left unresolved** —
  neither the audio-utterance model nor the description-scan model
  explains it, and the discipline this whole program follows is to say so
  rather than force a number.

**Bottom line**: this venue has at least two, possibly three, distinct
"how does the pack know" channels running simultaneously (base-rate prior,
text-metadata scanning, and one unexplained fast reaction) — a genuinely
different and more complex picture than WNT/MLB/WNBA's single-anchor-model
pack-lag tests. An audio-only detector, however fast, cannot beat a
description-scanner on words that leak into show notes — that's a real
constraint on this venue's tradeability that no amount of latency
engineering on our own end fixes.
