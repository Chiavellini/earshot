# KXSPECIALREPORTMENTION — status: NO-SIGNAL-YET

## What this was supposed to test

Whether a second newscast-shaped mention market (Fox News "Special Report
with Bret Baier") shows WNT's `UNBEATABLE` pattern (pack already at the
broadcast), MLB/WNBA's `CONTESTABLE` pattern (a real but slow pack), or
TBPN's `EMPTY-PACK` pattern (no live reaction at all).

## Two findings, both real, before any join was attempted

1. **This isn't a comparable newscast test after all.** `KXSPECIALREPORTMENTION-26JUN23`'s
   strikes all resolve on **Mark Rutte** (NATO Secretary General, a guest)
   specifically saying the word — `rules_primary`: "If Mark Rutte says
   <WORD> as part of Fox News: Special Report" — not on the show's own
   anchor/narration content generically. This is the same speaker-attribution
   problem already flagged and avoided in `rogan/pack_lag_rogan.py` (the
   Trump/"trans" market deliberately not used there). A non-diarized
   transcript match can't reliably tell Rutte's utterances apart from
   Bret Baier's questions or another guest's remarks.

2. **No public leg-1 reference is obtainable.** Fox News gates full
   "Special Report" episodes behind the Fox Nation subscription
   (`nation.foxnews.com/special-report-with-bret-baier-audio/`) — unlike
   ABC/CBS/NBC, there's no free full-episode YouTube VOD. Checked
   specifically for a standalone free clip of just the Rutte segment
   (networks sometimes post individual newsmaker interviews even when full
   episodes are paywalled) — none found. The only public artifacts are
   FoxNews.com text writeups quoting excerpts (e.g.
   [foxnews.com/media/nato-secretary-mark-rutte-praises-trumps-iran-strategy-addresses-presidents-frustration-allies](https://www.foxnews.com/media/nato-secretary-mark-rutte-praises-trumps-iran-strategy-addresses-presidents-frustration-allies)),
   not a timestamped transcript.

This is a real, structural fact about the venue class, not a search-effort
gap — directly relevant to `docs/EXPANSION.md`'s original, never-fully-tested
concern about cable-news content obtainability. Cable news generally, and
Fox News specifically, appears substantially harder to get free post-hoc
audio/transcript for than broadcast network news.

## What's here, ready for when a source appears

`pack_lag_special_report.py` — built, self-tested (5/5 checks), targeting
the 3 real single-mention settled-YES strikes with confirmed trade history
(RUSS: 46 trades 0.76→0.99; UKRA: 66 trades 0.78→0.99; OIL: 103 trades
0.52→0.99 — TRUM excluded as a count-strike, same discipline every prior
venue in this program applies). It refuses cleanly (`SystemExit` with a
clear reason) rather than running against nothing, per house discipline.
If a diarized transcript or Rutte-only clip is ever found for this
broadcast, drop `words.json` into `corpus/20260623/` and re-run.

## Trade timing note (observed, not yet explained)

All 4 strikes' first visible trades are already elevated (0.52–0.78, not
neutral 50%) starting ~18:46 UTC — over 3 hours before the show's own
6pm ET (22:00 UTC) air time. Possible explanations not investigated here:
pre-show wire coverage of the Rutte/Trump Iran story, an earlier same-day
Rutte appearance elsewhere, or general news-driven pre-pricing (the same
phenomenon already found on MLB's "Grand Slam" and Rogan's "Crazy"
strikes). Worth a look if a future pass revisits this venue.
