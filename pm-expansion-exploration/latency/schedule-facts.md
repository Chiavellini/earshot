# Schedule facts — replacing `slate/census.json`'s unreliable `cadence` field

Methodology: two independent legs. **Leg A** = real-world schedule (web
research / already-confirmed prior findings). **Leg B** = deep Kalshi
ticker-date history (`schedule-cross-check.py`, 200-event pulls per series,
not the census's 6-sample median-gap heuristic). Full data in
`schedule-facts.json` / `kalshi-history.json`. Fetched live 2026-08-05.

**Calibration check passed**: `KXWORLDNEWSMENTION` is the one candidate with
an already-confirmed real schedule (6:30pm ET daily). Leg B independently
reproduces it (near-daily cadence, all weekdays represented) — validates the
methodology before trusting it on the rest.

**A second, unplanned validation**: `KXMADDOWMENTION`'s Leg B data (19 of 23
events on Monday) independently reproduces Rachel Maddow's real-world
Monday-only format switch — found without assuming it in advance.

## Sorted by actionability

| candidate | class | schedule_confidence | actionability | note |
|---|---|---|---|---|
| `KXWORLDNEWSMENTION` | cable-news-nightly | confirmed | actionable-now* | *PARKED separately on linear-feed question, out of scope here |
| `KXMLBMENTION` | sports-league | confirmed | watch-for-next-open | in-season, nightly, 0 open at fetch — moving target, re-check near game time |
| `KXWNBAMENTION` | sports-league | confirmed | watch-for-next-open | in-season, near-daily |
| `KXMADDOWMENTION` | cable-news-nightly | confirmed | watch-for-next-open | fixed Monday 9pm ET, strongest cross-validation in the table |
| `KXMTPMENTION` | cable-news-sunday | confirmed | watch-for-next-open | 71% Sunday in Leg B, matches Leg A |
| `KXFTNMENTION` | cable-news-sunday | **leg-a-leg-b-disagree** | watch-for-next-open | Leg B skews Monday not Sunday — unresolved discrepancy, see below |
| `KXSECPRESSMENTION` | government-remarks | market-subset | watch-for-next-open | **correction**: more frequent (5-day median gap) than this repo previously assumed; no single fixed time-of-day found |
| `KXTBPNMENTION` | podcast | market-subset | watch-for-next-open | real schedule is daily; Kalshi board opens only ~every 12 days |
| `KXHANNITYMENTION` | cable-news-nightly | market-subset | dormant-fixed-schedule | nightly real schedule, sparse Kalshi listing |
| `KXCOLBERTMENTION` | cable-news-late-night | market-subset | dormant-fixed-schedule | zero Monday listings — see note |
| `KXKIMMELMENTION` | cable-news-late-night | market-subset | dormant-fixed-schedule | zero Monday listings — same pattern as Colbert |
| `KXSPECIALREPORTMENTION` | cable-news-nightly | confirmed-thin-sample | dormant-fixed-schedule | only 2 data points |
| `KXNFLMENTION` | sports-league | confirmed-out-of-season | dormant-fixed-schedule | 2026 season starts 2026-09-09; correctly dormant now |
| `KXNHLMENTION` | sports-league | confirmed-out-of-season | dormant-fixed-schedule | off-season until ~October |
| `KXROGANMENTION` | podcast | confirmed-not-fixed | dormant-fixed-schedule | Kalshi board essentially dead (2 events, 10+ months ago); Polymarket is the live venue for this class |

## Flags worth resolving before relying on this table further

1. **`KXFTNMENTION` vs `KXMTPMENTION` — same show-class, opposite Leg-A/Leg-B
   agreement.** Face the Nation's ticker dates skew Monday despite airing
   Sunday; Meet the Press's ticker dates correctly skew Sunday. Either FTN's
   ticker-date convention differs from MTP's, or one of the two shows'
   assumed air day is wrong. Not resolved here — resolve before scheduling
   any capture against either.
2. **Colbert and Kimmel both show zero Monday listings** despite airing
   Mon-Fri — consistent across two independent late-night shows, so
   plausibly a real pattern (lower newsworthy content on Monday episodes?
   a shared Kalshi listing convention?) rather than noise. Unexplained.
3. **`KXSECPRESSMENTION`'s ticker history extends to 2026-12-31** — Kalshi
   has pre-listed event tickers far into the future for this series. Worth
   checking whether those are confirmed briefings or placeholder/templated
   listings before treating the far-future dates as real schedule signal.
4. **MLB/WNBA's `open_events_now: 0`** is a fetch-moment snapshot, not a
   standing fact — Kalshi's `/events` objects carry no `status` field at
   all (verified live), so "open" can only be determined by which
   status-filtered query returns a ticker. At 2026-08-05 mid-afternoon UTC,
   Aug 1 games were already finalized and evening games' markets likely
   hadn't posted yet. Re-check near actual game time before concluding
   these venues are inactive.
5. Several `leg_a_real_schedule` entries above are marked
   `high-general-knowledge-not-live-reverified` (Hannity/Special
   Report/Colbert/Kimmel/MLB/WNBA/NHL airtimes) — these are long-stable US
   broadcast facts, not fabricated, but this pass did not spend a fresh
   WebSearch confirming each one individually the way `KXSECPRESSMENTION`
   and the earlier `KXWORLDNEWSMENTION`/TBPN candidates were. Flagged per
   house discipline rather than silently presented as equally certain.
