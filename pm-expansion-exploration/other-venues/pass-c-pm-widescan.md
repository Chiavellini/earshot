# Pass C — Polymarket Gamma widescan (live, read-only)

Goal: find "mention"-style Polymarket markets beyond the single
`tag_slug=mention-markets` pull already recorded in `CAPTURE-PIPELINE.md`
(22 open events, 3 of them earnings). `pm-map-build.py` has no discovery
mode today — this is genuinely new ground.

## What was run

1. **Volume-sorted sweep**: `GET gamma-api.polymarket.com/events?closed=false&limit=100&offset=N&order=volume24hr&ascending=false`,
   paginated N=0..2000 (21 pages, 2100 events scanned). **Hard pagination
   wall found**: offset=2100 returns `HTTP 422 Unprocessable Entity` — Gamma
   caps `offset+limit` for this endpoint (exact cap unconfirmed, but 2100 is
   the first failure). Worth pinning if this lane keeps using this endpoint.
   Title-regex (`say(s)?|mention|word(s)?|during (his|her|their)|speech|utter`)
   over all 2100 scanned events found **10 hits, all already in the known
   22-event tag list** — confirms the volume-sorted sweep, at least down to
   ~$1,600 in `volume`, essentially reproduces `tag_slug=mention-markets`
   and finds nothing new. Negative result, stated plainly: broad volume
   sweeping is not where new markets hide.
2. **`public-search` targeted queries** (9 queries: "Powell say", "hearing
   testimony say", "press conference say", "Congress hearing mention",
   "Senate testimony", "Fed chair say", "debate mention word", "SCOTUS
   say", "governor say") — **this is where new markets surfaced**. Search
   returns closed/historical markets too (not just `closed=false`), which
   is actually useful here: it reveals the *recurring pattern* a single
   open-events snapshot can't show.

## New findings (beyond the 22-event open tag list)

**FOMC volume, concretely pinned**: PM runs "What will Powell say during
[Month] Press Conference?" every meeting. Closed-market volumes found:
June $5.48M, September $1.55M, April $1.21M, December $1.06M, January
$937K. This is an order of magnitude past anything in the earnings class
and directly substantiates `docs/EXPANSION.md`'s "the crown" framing with
real numbers — worth citing in the scoring pass.

**Genuinely new pattern — Senate confirmation hearings**: `what-will-kevin-
warsh-say-during-senate-nomination-hearing` and two more Warsh "press
conference" markets ($15K, $178K, $311K volume). Confirmation hearings are
a distinct venue class from `KXHEARINGMENTION`'s general committee-hearing
framing in `docs/EXPANSION.md` Tier 2 #4 — narrower, event-specific,
possibly higher engagement per event. **Not checked against Kalshi's
census for a counterpart** — that's the next step, not done here (out of
this pass's scope).

**Ad-hoc bilateral/press-conference markets with real volume**: Trump with
Netanyahu ($168K), Turkey ($22K), Carney/Canada ×2 ($760K, $247K), "Fed
Chairman announcement" ($205K), plus recurring "What will Trump say [this
week/in March]" monthly/weekly rollups ($450K-$1.23M). These substantiate
candidate-longlist.md's cluster F (one-off named events) as real-volume,
not speculative — and confirm the pattern is genuinely recurring (Trump
gets a fresh press-conference market for essentially every notable
bilateral meeting), not a handful of one-offs.

**TBPN corroborated independently**: `what-will-jim-cramer-say-during-tbpn-on-december-4`
confirms candidate-longlist.md's Pass-A guess that TBPN is a real,
Kalshi-adjacent-worthy PM venue (found here via search, not the tag list).

## Kalshi-counterpart question — not resolved in this pass

I did not cross-check every new slug above against Kalshi's series list
(that's Pass B / the checklist stage's job, out of scope for a Gamma-only
sweep). Flagging explicitly for that stage: the Warsh Senate-hearing market
and the individual Trump bilateral-event markets (Netanyahu/Turkey/Carney)
are the most likely candidates for **no Kalshi counterpart at all** — if
confirmed, PM-only markets need new execution code from scratch (Kalshi's
`OrderTransport` is a closed, Kalshi-only 2-variant enum; no Polymarket
order code exists anywhere in this repo), a materially different, larger
lift than adding a capture-only config for an already-Kalshi-mirrored word.

## Bottom line

The tag_slug pull was NOT complete, but not because of missed *open, live*
markets in the mention-markets tag itself (the volume sweep confirms that
part was fine) — it's incomplete because Gamma's tag taxonomy doesn't
retroactively surface the *pattern* of recurring, high-volume markets
(FOMC, Trump bilaterals, Senate hearings) that only becomes visible by
searching historical/closed instances of the same recurring title
template. For future discovery passes: search closed markets for a
person/venue's history, not just today's open snapshot, to find recurring
classes worth pre-positioning for next time they open.
