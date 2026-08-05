# NHL — sports commentator feed accessibility (lighter-touch, off-season)

**Season status**: NHL regular season does not start until October 2026 —
currently off-season as of 2026-08-05. No live decodability/simulcast
testing done in this pass; desk research only, to prep for when testing
becomes possible.

## Rights structure

Confirmed (live search): the NHL's marquee TV deal is the 2026-27+
12-year Rogers Communications agreement (~$11B CDN) covering Canadian
national rights across TV/digital/streaming, explicitly moving toward
**fewer regional blackouts** under the new cycle. This is a national-TV
deal, not radio-specific. No source found breaking out local NHL radio
rights as a structurally separate licensing track the way MLB's
radio-vs-TV blackout split was confirmed distinct in a prior pass — treat
NHL's radio/TV rights relationship as **unconfirmed**, don't assume it
mirrors MLB's.

## Stream discovery — a strong, concrete finding

**`NHL.com` itself directly offers live game radio through TuneIn**
(`nhl.com/info/team-audio`), described as free access to **every NHL
matchup, home and away team calls, for the full season including
playoffs and the Final** — an official league-sanctioned distribution
partnership, not just an aggregator scraping a signal. Critically: **this
is free and available "from anywhere in the world," with one specific,
notable exception — live game coverage is explicitly NOT available to
listeners physically located IN CANADA** (Rogers' exclusive Canadian
rights carve it out). This is close to the geo-inversion hypothesis the
plan flagged as worth testing: the restriction here runs the opposite
direction of "needs a US presence" — it's a non-Canada restriction, and a
non-US, non-Canada vantage (or a US one) should both be unaffected.

Secondary channels also found: Audacy (`audacy.com/sports/nhl-1173b`) and
SiriusXM (`siriusxm.com/sports/nhl`) both carry NHL radio/podcast content
— SiriusXM specifically for Stanley Cup Final play-by-play — but TuneIn's
direct NHL.com partnership is the strongest, most official, most clearly
free-and-global candidate.

## Not yet done (needs the season to start)

- `scripts/venue-probe.sh` decodability test against a real TuneIn NHL
  stream URL.
- Simulcast identity verification (confirm the stream is the actual live
  game call, not a studio/highlights show) — the mandatory WNT-lesson
  check.
- Actual geo-behavior test from available vantages (only the Canada
  carve-out is documented; US/non-US-non-Canada behavior unconfirmed).

## Season timing

NHL 2026-27 regular season starts **October 2026** (exact date not pinned
in this pass). Revisit this candidate once the season opens and a live
game is available to test against.
