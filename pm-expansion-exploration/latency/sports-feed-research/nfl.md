# NFL — sports commentator feed accessibility (lighter-touch, preseason)

**Season status**: the 2026 NFL regular season starts **September 9,
2026** (confirmed in prior research today) — as of 2026-08-05 the league
is in preseason-only territory. No live decodability/simulcast testing
done in this pass; desk research only.

## Rights structure — a strong, concrete finding

**SiriusXM is the exclusive NATIONAL distributor of NFL radio broadcasts
in the United States** — but critically, **teams separately hold
in-market agreements with their own local radio stations**, i.e. the same
"national TV-style rights vs. local radio rights" split structurally
confirmed for MLB shows up here too, just with SiriusXM as the national
radio aggregator rather than a TV network.

**Best finding of this pass: TuneIn holds an INTERNATIONAL AUDIO
DISTRIBUTION DEAL directly with the NFL**, announced 2022
(`businesswire.com/news/home/20220908005206`, "TuneIn inks international
distribution deal with the NFL" / confirmed via a second independent
source, IMDb news syndication of the same release, "TuneIn gets
international audio rights to NFL games"). This is a league-level,
official international audio-rights deal — not a scraped/unofficial
stream — which is exactly the structural pattern the plan was looking
for: audio rights licensed and distributed separately from (and more
permissively than) domestic TV/video rights. This deal predates 2026 but
no evidence found that it lapsed; worth a fresh confirmation once
preseason/regular-season games are live to test against.

iHeartRadio is a secondary channel: "many affiliate stations carrying NFL
broadcasts are available through the iHeart app" — useful if a specific
team's flagship station happens to be in the iHeart network, but this is
per-team/per-affiliate, not a single league-wide deal the way the TuneIn
one is.

## Blackout considerations

Not resolved in this pass — general commentary confirms blackout/market
restrictions exist and vary by service and team agreement, but no
NFL-specific radio blackout policy document was found (unlike MLB, where
a policy document was located directly). Worth a dedicated follow-up
search once live testing is possible.

## Not yet done (needs the season to start)

- `scripts/venue-probe.sh` decodability test against a real TuneIn NFL
  stream URL (once a preseason or regular-season game gives something to
  test against).
- Simulcast identity verification.
- Geo-behavior test (no Canada-style carve-out documented yet for NFL,
  unlike NHL — unconfirmed either way).

## Season timing

2026 NFL regular season starts **September 9, 2026**; preseason games may
already be occurring as of this research date (2026-08-05) and could be a
lower-stakes earlier test opportunity if TuneIn carries preseason audio —
not checked in this pass.
