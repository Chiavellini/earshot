# Source-feed latency investigation

Scope: does any fixed-schedule mention-market venue have a demonstrably
low-latency source feed — and specifically, is a low-latency sports
commentator feed obtainable at all, given the existing "linear-TV blocker"
verdict for sports was never independently measured. Read-only /
research-and-new-code-only against `pm-expansion-exploration/latency/`;
nothing under `configs/`, `scripts/` (existing), `src/`, `docs/`, `state/`,
or `slate/` is ever modified.

## Pre-registered decision rule (stated before any number below is used to decide anything)

Two decision rules already exist in this codebase, use **different metrics
with different sign conventions**, and must never be silently conflated:

1. **`scripts/wnt-pack-lag.py`'s own `offset` rule** (production, read-only
   reference): `offset` = how far behind (positive) or ahead of (negative)
   the real-world event the trading pack's reaction sits, from a slope-fit
   join of a leg-1 timing reference against the market's own sweep onsets.
   `worst offset < 15s` ⇒ the pack is already AT the broadcast — no feed
   can win, close the lane. `median offset > 20s` ⇒ the pack is materially
   delayed — a live feed is worth trying. Otherwise: MARGINAL, judge the
   distribution, not one number.
2. **`docs/COMPETITOR-ANATOMY.md`'s `Δrace` convention**: a *different*
   quantity (our-detection-relative-to-sweep, not pack-relative-to-event),
   opposite-signed thresholds (`≥−2s` contestable, `≤−5s` dead).

**Every result below is reported against rule (1)**, since every pack-lag
build in this investigation follows `wnt-pack-lag.py`'s own pattern
directly (leg-1 timing reference × Kalshi/PM sweep onset × slope-fit). Rule
(2) is not used here and should not be retrofit onto these numbers without
restating them in Δrace's own terms first.

## What's here

- `schedule-facts.json`/`.md` — real-world-vs-Kalshi-ticker-history schedule
  facts, replacing the unreliable `slate/census.json` `cadence` field.
- `lib/kalshi_sweep.py`, `lib/slope_fit.py` — the generic two-clock join +
  refuse-don't-fabricate core, pattern-extracted from `wnt-pack-lag.py`,
  reused by every venue build below.
- `mlb/`, `wnba/` — pack-lag builds, run against real settled games.
- `tbpn/` — leg-1 (VOD/caption) build only, banked for the next join.
- `rogan/NOTES.md` — Polymarket trades-depth verification.
- `sports-feed-research/` — MLB/WNBA/NHL/NFL commentator-feed accessibility.
- `latency-scoreboard.md` — the final synthesis.

## Headline finding

**Sports does not show WNT's failure mode.** WNT's own trading pack reacts
at essentially zero offset (median −6.31s — already at the broadcast, no
feed can beat it). MLB's one measured strike (a fielding error) shows the
pack reacting **+140.56s** late; WNBA's three measured strikes (traveling,
technical foul, across 2 games) show **+12.0s / +16.6s / +26.4s**, slope
0.997 (the join is trustworthy). Both are small samples, but both point the
same direction: **the existing sports mention-market pack is not already
unbeatable, unlike WNT's.** The blanket PARKED verdict inherited by
analogy from WNT does not hold up against real, if early, data.

See `latency-scoreboard.md` for the full table and every caveat.

## Update 2026-08-05 (later same day): live capture test, before spending anything

Per user direction, moved from research to an actual live-capture test using
the real system — see `mlb/LIVE-CAPTURE-STATUS.md` for full detail. Headline:
a real, free (no subscription) MLB radio stream was resolved and verified
working (KNBR 680, Giants, Triton Digital CDN), and the full mechanism —
`capture-bakeoff` → wall-clock anchor → `earshot replay` (confirmed, at the
Rust type-system level, structurally incapable of placing an order) → real
`PipelineReport` with genuine detection-latency numbers — was built and
shaken out end-to-end successfully. The only blocker is external: Kalshi has
not listed a new `KXMLBMENTION`/`KXWNBAMENTION` event since 2026-07-30. A
poll script (`poll_new_market.py`, since generalized to also watch
`KXTBPNMENTION` — see the Update below) is ready to catch the moment one
reappears, at which point the live three-way comparison (our detection vs.
the real event vs. the market's price pump) can run immediately.

## Update 2026-08-06: is TBPN's pattern general? Scaled to 9 venues

Consolidated the reusable pack-lag core (`lib/phrase_match.py` — three
independent, drifting copies of the word-matching logic across WNT/TBPN/
Rogan unified into one canonical module; `lib/pm_sweep.py` moved from
`rogan/` since it was always venue-agnostic), then triaged 93 Kalshi
candidate series + a wide Polymarket sweep for real, substantial trade
history (33 Kalshi + ~15 PM classes confirmed real), and deep pack-lag
tested 6 new venues spanning distinct formats: Powell (misc. appearance),
Keir Starmer's UK Parliament PMQs, the State of the Union address, Fox's
Special Report, a Netanyahu interview, and an NVIDIA keynote.

**Headline finding**: none of the 6 showed WNT/MLB/WNBA's clean
slope≈1.0 "real pack reacting live" signature. State of the Union and the
NVIDIA keynote reproduced TBPN's exact `EMPTY-PACK` batch-settlement
pattern on completely different, higher-stakes venues (including the
single highest-profile one-off political event tested all investigation
long) — so TBPN's pattern is not a one-off. Netanyahu's interview came back
`GAMED` even more starkly than Rogan (zero trading after the interview
aired at all — the market was fully priced on predictable talking points
before broadcast). The rest hit real data-availability walls (no
trustworthy wall-clock anchor, or no obtainable source) rather than
resolving the venue-behavior question either way.

**The real, generalizable answer**: it's not venue *format* that predicts
exploitability — it's whether the resolution criterion is a **rare,
discrete, structurally unpredictable event** (MLB/WNBA's fielding
errors/fouls — the only two `CONTESTABLE` results found) versus a
**common, predictable word/topic** (which markets price from priors alone,
`GAMED`, no speed helps) or a **whole-show/whole-speech review** (`EMPTY-PACK`,
nothing to race in real time). See
`mention-market-exploitability-scoreboard.md` for the full table, the
42-candidate backlog, and the methodological lessons this round surfaced.

## Update 2026-08-07: pooling MLB/WNBA past n=1/n=3

Per user direction, prioritized strengthening the two proven `CONTESTABLE`
wins before spending more effort on the 42-candidate backlog — both were
still single-digit samples, not trustworthy population estimates.

**MLB** (`mlb/pool_more_games.py`, new): scanned all 121 settled
`KXMLBMENTION` events (essentially the full available history). Result is
more qualified than the n=1 finding suggested, not simply confirmed:
- `ERRO` only cleanly matches a real Error play in 16 of 80 attempts (20%)
  — the other 80% show the same GAMED signature as `GRAN`/`WALK` (word said,
  no matching event; or pre-priced before the game started). A live
  counter-example: `KXMLBMENTION-26AUG01AZCLE` settled `ERRO=yes` with real
  volume on a game whose play-by-play contains zero Error plays at all.
- `WILD` is 0 for 34 — every single checked settlement is GAMED. Reclassify
  it alongside `GRAN`/`WALK`/`BUNT`, not as a second verified-live code.
- The 16 clean `ERRO` points are bimodal: a fast cluster (n=12, 10-141s,
  median 45s) and a slow cluster (n=4, 429-1758s, median 790s) — mechanism
  unconfirmed (a plausible guess is retroactive official-scorer error
  rulings, not verified).

**WNBA** (`wnba/discover_games.py`, new; `wnba/pack_lag_wnba.py`'s
`KNOWN_GAMES` grown 2→12 of 14 total settled events): pooling surfaced and
fixed a real bug, not just added data — when a detected sweep predates
every trusted play-type occurrence in a game, the old code silently forced
the pairing onto the first occurrence regardless of distance, producing
multi-thousand-second garbage deltas. Fixed to refuse that pairing instead
(4 of 12 games' strikes now refused this way — itself a finding: roughly a
third of checked TRAV/TECH settlements look GAMED, not live-reactive).
Remaining n=9 pairs: the raw pooled slope-fit refuses (slope=1.153),
dragged by one outlier (+3240.6s, flagged not explained). Dropping it:
n=8, slope=1.0034 (clean pass), offset +17.2s, spread +12.0 to +71.5s —
tightens the original n=3 finding (+12 to +26s) rather than overturning it.

**Net effect on the scoreboard**: both venues keep their `CONTESTABLE`
verdict, but the addressable surface is narrower than n=1/n=3 implied —
only a minority of strike-settlements per venue are genuine live reactions,
and a full accounting requires checking each strike code's own hit rate,
not trusting the venue label. See the updated MLB/WNBA rows and the two new
"Open methodological items" in `mention-market-exploitability-scoreboard.md`.

**Backlog triage** (`mlb/pool_more_games.py`'s sibling for the backlog side —
`latency/backlog_strike_triage.py`, new): pulled every remaining Kalshi
backlog ticker's REAL, current `rules_primary`/`yes_sub_title` text (30
tickers, 440 strikes, reusing `scripts/market-phrase-screen.py`'s new
`live_market_detail()`) instead of ranking by ticker name. Result: **99.8%
of strikes (439/440) are generic political/topical vocabulary** — the same
`GAMED` shape already proven unbeatable by Netanyahu/Rogan. Recommend
deprioritizing the entire 30-ticker Kalshi "person mention" backlog family;
it cannot structurally produce a `CONTESTABLE` result the way MLB/WNBA did.

The lighter Polymarket pass found one genuinely promising exception: the
NFL "announcer mentions" class (verified live against a real settled event,
Panthers vs. Cardinals Hall of Fame Game) mixes rare discrete play types
("Safety", "Late Hit", "One Handed" catch — MLB/WNBA's exact shape) with
predictable vocabulary ("Mahomes", "Super Bowl") — the only backlog
candidate confirmed, by real strike text, to share the winning shape.
**Recommended next build: `nfl/pack_lag_nfl.py`**, following WNBA's
ESPN-play-by-play template. See the scoreboard's Backlog section for the
full per-ticker table and the remaining 13 Polymarket classes (not
individually re-verified this round — expect the same GAMED shape by
analogy, unchecked).

## Update 2026-08-07 (later same day): fill availability — a different question than racing

User pushback, correctly: "priced ahead of time" isn't automatically a
dead end. If a sub-97c fill is available the moment our own detector
confirms a word/event, that's a profitable buy-and-hold trade regardless
of whether "the market saw it coming" — no race needs winning. Every
verdict in this program up to this point (`CONTESTABLE`/`GAMED`/
`EMPTY-PACK`) was built around reaction SPEED (`sweep_onset()`'s slope≈1.0
race framing), never checked for FILL AVAILABILITY. New tool: `lib/
fill_window.py` — given a trade tape and a confirmation timestamp, reports
whether a sub-ceiling price was ever tradeable afterward, distinct from
when/whether the market finally swept to $1.

**Two real bugs found and fixed in `lib/pm_sweep.py`** while building this
check (see the scoreboard's "Fill availability" section for full detail):
an outcome-side price inversion (No-side trades' `price` was used directly
instead of `1 - price`, corrupting every Polymarket venue's `_yes` series)
and silent 500-trade truncation with no pagination (SOTU's ALIEN strike
actually has 548 trades; the missing 48 could masquerade as a real sweep
onset). Both fixed, both self-tested. This means Rogan/SOTU/keynote's
*original* verdicts, not just today's new check, needed re-verification —
done: Rogan's `GAMED` verdict survives untouched; SOTU mostly survives
but the mechanism/timing was wrong (sweeps were dated relative to a
truncated window, not the real one); **the NVIDIA keynote's verdict does
NOT survive** — with both bugs fixed, all 5 strikes (previously only 2/5
even matched) show a consistent ~103-106 minute reaction lag that tracks
each individual utterance (slope=1.030, just outside the formal 0.02
tolerance) — structurally much closer to MLB/WNBA's shape than to the
originally-reported "GAMED, priced a day ahead."

**Fill-availability results, all re-run with the fixed library**: TBPN (4/4
strikes), the NVIDIA keynote (5/5), and MLB's `GRAN`/`WALK` (the original
GAMED counter-examples) all show real, minutes-to-hours-long windows where
price sat well below the 97c ceiling after confirmation, before eventually
sweeping. 2 of WNBA's 4 dropped pairs look recoverable the same way. The
genuine dead ends are narrower than "GAMED"/"EMPTY-PACK" as a whole:
literal zero-post-confirmation-liquidity (Netanyahu, one WNBA pair) or
price already at ceiling before confirmation is even possible (most of
SOTU) — not "predictable" or "batch-settled" by themselves.

**This reopens the 30-ticker Kalshi backlog "deprioritize" call from
earlier today** — that classification was done purely from `rules_primary`
text (predictability), never checked for fill availability, and today's
results suggest that check would plausibly recover some of it. Flagged as
the clear next step, not yet done. See the scoreboard's "Fill availability"
section for the full per-venue table and both bug writeups.
