# Multi-event PM capture pipeline

Reuses `scripts/xvenue-autorun.sh` **completely unmodified**. All output
lands inside `pm-expansion-exploration/captures/`, never `state/xvenue/`.
`--check` (offline self-test, 7 checks) passed 2026-08-05.

## Finding, 2026-08-05: PM's earnings-mention coverage is much narrower than the historical captures suggested

Checked `GET gamma-api.polymarket.com/events?closed=false&tag_slug=mention-markets`
live (2026-08-05, 22 open events total) against today's remaining Kalshi
earnings slate (`KXEARNINGSMENTIONDASH/TLN/DUOL/FIG/MELI/EBAY-26AUG05`, all
still ahead as of 17:26 UTC). **None of the six have a Polymarket mirror
right now.** Of the 22 open PM "mention" markets, only 3 are earnings at
all: Uber and Disney (both already called this morning, 12:00Z/12:30Z —
too late to capture), and Airbnb (`ABNB`, tomorrow). The other 19 are
Fed-adjacent/political/entertainment (Karoline Leavitt, Trump ×4, MrBeast,
NYT front page, Elon, Joe Rogan, All-In, Big Brother ×4, House of the
Dragon, President Curtis) — see `other-venues/` for what that implies for
Task 2.

This means the "many events" capture plan can't simply run against
whatever Kalshi is arming that day — it has to run against the much
smaller set Polymarket actually bothers to list. Recommend re-running this
same Gamma query each morning (a natural fit for `xvenue-daily.sh`'s
existing health-check cadence) rather than assuming coverage.

## Live opportunity found and staged: ABNB, 2026-08-06

`configs/webcast-abnb-26aug06-arm-wc.toml` (production's own template) has
its call time as an unresolved `TODO`. Sourced independently
(stocktitan.net, investors.airbnb.com, Yahoo Finance, cross-checked
2026-08-05): **2:00pm PT / 5:00pm ET Aug 6, 2026 = 21:00 UTC**. Recorded in
`call-times.json` (this exploration's own file — never written back to
`slate/call-times.json`).

Map built and verified:
```
python3 scripts/pm-map-build.py \
  --out pm-expansion-exploration/maps/pm-map-abnb-26aug06.json \
  --call-times pm-expansion-exploration/call-times.json --strict \
  "KXEARNINGSMENTIONABNB-26AUG06=what-will-airbnb-say-during-their-next-earnings-call-20260803163357333"
# -> 11 markets, clock=word, vol=$1,618 — GRAB guard passed (PM end_date
#    2026-08-06T23:59Z is after the 21:00Z call)
```

**Ready-to-run command** (not yet started — see "Decision needed" below):
```
scripts/xvenue-autorun.sh \
  --event KXEARNINGSMENTIONABNB-26AUG06 \
  --map pm-expansion-exploration/maps/pm-map-abnb-26aug06.json \
  --secs 7200 \
  --out pm-expansion-exploration/captures \
  --call-times pm-expansion-exploration/call-times.json
```
Start ~20:45 UTC 2026-08-06 (15 min before the call) so the recorder is
live before the first word. `--secs 7200` (vs. the script's 5400s default)
buffers Q&A overrun — the whole point of this pipeline is finally getting
*full* call coverage, unlike the two historical gappy captures (AMD 3.7%,
PLTR 63.5%).

**Decision needed, not made autonomously**: this is a ~2-hour, tomorrow-
timed background job. This session has no mechanism to wait 24h and fire
it (long sleeps are blocked, and wiring cron/launchd automation was
explicitly scoped OUT of this exploration per the approved plan — "stays
manual/on-demand... wiring real automation would itself be a quasi-
production change and should be a separate, reviewed decision"). Options:
(a) the user runs the command above manually tomorrow, (b) the user asks
this session to schedule it via the `schedule` skill (one-time cloud-agent
trigger), or (c) it's picked up in a future session. Recommend (a) or (b)
over silently doing nothing.

## Recipe for every future event (repeat this, don't automate it yet)

1. **Check PM coverage first** — `curl -s "https://gamma-api.polymarket.com/events?closed=false&tag_slug=mention-markets&limit=200"`
   and grep for the issuer name. Most Kalshi earnings events will have NO
   match (see finding above) — that's expected, not a bug.
2. **Confirm the call time independently** if production's own config
   still shows a TODO (`grep starts_at configs/webcast-*.toml`) — a quick,
   sourced web check, same discipline `docs/HUNT.md`'s IR-release scraping
   already applies, just done by hand here.
3. **Build the map**: `pm-map-build.py --strict` as above. `--strict`
   refuses outright (not just warns) if the PM market's `end_date` predates
   the Kalshi call — the GRAB-class guard.
4. **Start the recorder** 10-15 min before call time, `--secs 7200`,
   pointed at `pm-expansion-exploration/captures`.
5. **Spot-check the first captures**: `pm-tape.py`'s push recorder (the
   newer, ms-timestamped feed) has never run across a real full event
   before this exploration, only bench-tested for 40s — verify the process
   is alive and the tape file is growing during the first 2-3 live runs
   before trusting it unattended.
6. **After the call**: run `profitability/extract_decisions.py` against
   `state/<EVENT>-WEBCAST/` **only if production independently armed this
   same event** (this exploration never arms anything itself) — that's
   what unlocks `reanchor_takeable.py`'s anchored analysis for that event's
   words. Every capture, armed or not, still feeds
   `profitability/book_shape_survey.py`'s broader base-rate analysis.
