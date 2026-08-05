# Latency scoreboard — synthesis

Every number tagged `measured` / `estimated` / `unknown`. Verdict vocabulary
matches this project's refusal-first house style
(`scripts/xvenue-report.py`'s `NOT_RESOLVABLE`/`UNSTABLE`/`VOID`): a venue
gets `CONTESTABLE` only when a real join produced a trustworthy (slope≈1.0)
offset showing daylight; `UNBEATABLE` only when the same join shows the pack
already at the event; `WATCH` when the schedule is fixed but no market/feed
work has cleared yet; `NO-SIGNAL-YET` when nothing conclusive exists.

## Master table

| venue_id | schedule | market state | pack-lag result | feed accessibility | verdict |
|---|---|---|---|---|---|
| `KXWORLDNEWSMENTION` | measured — 6:30pm ET daily | active, thin | **measured**: offset median −6.31s (n=6 sessions, `VERIFICATIONS.md` Q-WNT-PACK) — pack already at broadcast | linear-TV, RF/antenna closed; internet radio never tested (see caveat below) | `UNBEATABLE` (as previously found; not re-litigated here) |
| `KXMLBMENTION` | measured — near-daily in-season (Mar–Oct) | in-season, snapshot-dormant (re-check near game time, not a real absence) | **measured, n=1**: Error strike, offset **+140.56s** (2026-08-01 game) — pack reacts >2min after the play | MLB+ audio: $59.99/yr, no blackouts, international — **estimated-accessible**, stream URL not yet resolved (dynamic player, needs browser trace) | `CONTESTABLE` — encouraging but underpowered; pool more games/strike-types before trusting |
| `KXWNBAMENTION` | measured — near-daily in-season (May–Oct) | in-season, snapshot-dormant (same artifact as MLB) | **measured, n=3**: Traveling + Technical Foul, slope 0.997, offsets +12.0s/+16.6s/+26.4s (2 games, 2026-08-01) | thin — only 2/13 teams' flagship radio stations found (likely a search gap); League Pass confirmed no blackout outside US/Canada (video, not audio) | `CONTESTABLE`, lower confidence than MLB on the feed side |
| `KXTBPNMENTION` | measured — real schedule daily (weekdays 11am–2pm PT), Kalshi board opens ~every 12 days | 12 historical settled events; currently 0 open | **measured, n=4** (ANTH/ROBO/HELI/OPEN, `KXTBPNMENTION-26JUL23`): fit REFUSED (slope −0.02, not ≈1.0) — all 4 sweeps landed in a ~500s window near the show's END (5746–6254s of 6566s) regardless of when each word was actually said (spread 360–6250s). Real finding, not a failed join: **this market does not react to individual mentions in real time; trading clusters near the end of the show.** | n/a — YouTube, already-proven ingress pattern | `NO-SIGNAL-YET` for a real-time race; structurally end-of-show-batched, not utterance-triggered |
| `KXROGANMENTION` (Kalshi) | confirmed not fixed / Kalshi board essentially dead (2 events, 10+ months old) | dormant on Kalshi | not attempted (no Kalshi market to join against) | n/a | `NO-SIGNAL-YET` on Kalshi |
| Rogan (Polymarket) | podcast, weekly board format confirmed real | **active/settled** — used "What will be said on the first JRE episode of the week (July 27)", closed 2026-08-02 | **measured, n=3** (SPY/ALIEN/CRAZY): all 3 "pre-priced," but the raw timelines are the finding: **CRAZY** repriced to 0.93 a full DAY before the episode published (base-rate pricing off a filler word, same phenomenon as MLB's Grand Slam/Walk Off); **ALIEN** jumped to 0.99 within 2 min of publish because the word sits in the video's own text DESCRIPTION ("Alien Sharks..."), readable with zero audio; **SPY** also jumped within 2 min of publish with no text-metadata explanation found — left unresolved rather than guessed at | n/a | `NO-SIGNAL-YET` for a real-time race; this venue has ≥2 non-audio "how the pack knows" channels (base-rate priors, description-scanning) that beat any audio detector outright on words that leak into show notes |
| `KXSECPRESSMENTION` (WH briefings) | **corrected**: real schedule more frequent than assumed (~5-day median gap, 59 events in Leg B history), not confirmed ad-hoc | market-subset of real schedule | not attempted | not researched this pass | `NO-SIGNAL-YET`, but schedule correction makes this worth a future pass |
| `KXMADDOWMENTION` | measured — Monday 9pm ET (83%/19 of 23 events Monday in Leg B, independently reproducing her real-world format switch — found blind, unplanned validation) | market-subset, dormant-fixed-schedule | not attempted | not researched this pass | `NO-SIGNAL-YET` |
| `KXMTPMENTION` / `KXFTNMENTION` | Sunday shows; MTP's Leg A/B agree (71% Sunday), **FTN's disagree** (skews Monday in Kalshi history despite airing Sunday) — unresolved discrepancy | market-subset, dormant | not attempted | not researched this pass | `NO-SIGNAL-YET`; resolve the FTN discrepancy before any capture |
| `KXNFLMENTION` | confirmed out-of-season — 2026 season starts 2026-09-09 | dormant, correctly so | not attempted (no live season) | TuneIn holds a confirmed international NFL audio-rights deal (2022), structurally separate from SiriusXM's US exclusivity and local team-radio deals — **estimated-promising**, untested | `WATCH` — revisit in September |
| `KXNHLMENTION` | confirmed out-of-season — starts ~October 2026 | dormant, correctly so | not attempted | **strongest single feed finding of the investigation**: NHL.com itself directly offers free global game radio via TuneIn, home+away calls, full season+playoffs — the only restriction is exclusion in Canada, not the US. No US presence required at all. | `WATCH` — revisit in October; highest-priority sport once in-season |
| Cable news (Hannity/Colbert/Kimmel/Special Report) | measured-but-thin — real schedule is fixed nightly, Kalshi listing sparse; Colbert/Kimmel both show zero Monday listings, unexplained | dormant-fixed-schedule | not attempted | not researched this pass | `NO-SIGNAL-YET` |

## What changed the picture

**The core question the user asked — "is the sports blocker real" — has a
first real answer, not just a reframing.** `docs/EXPANSION.md`'s PARKED
verdict for sports rested entirely on analogy to WNT, and WNT's own
decisive finding (Q-WNT-PACK) was never run against a sports market until
today. It now has: MLB shows a pack reacting >2 minutes late on the one
measured play; WNBA shows a pack reacting 12–26 seconds late across three
plays in two games, on a trustworthy (slope 0.997) join. Neither result
resembles WNT's near-zero offset. **This does not yet prove sports is
tradeable** — n=1 and n=3 are not population estimates, and most mention
strikes on both boards turned out to be pure word-choice markets with no
structured-event proxy at all (only rare discrete-event strikes like
"Error" or "Traveling" could be matched against a play-by-play feed this
cheaply) — but it directly contradicts the assumption that closed the lane,
using the project's own established, zero-live-capture method.

**On the radio-feed question specifically**: internet-streamed radio was
confirmed to have never been tested anywhere in this codebase before today
(the "RF/AM/FM KILLED four ways" verdict tested physical antenna hardware,
an HD Radio delay quirk, and one slow satellite app — never a direct team
or league webstream). Today's research found concrete, promising,
previously-unknown paths for three of four sports checked: MLB (a genuine
no-blackout, internationally-purchasable audio product, $59.99/yr, though
the actual stream URL isn't extracted yet), NHL (a free, global,
league-endorsed TuneIn deal excluded only in Canada), and NFL (a
confirmed-but-untested international TuneIn rights deal). WNBA is the one
weak spot — its local-radio footprint looks thin, though this may be an
artifact of a shallow search pass rather than a real absence.

**On TBPN and Rogan — a coherent, cross-venue negative finding.** Both were
targeted specifically because they're testable TODAY (settled data already
exists, no live-market wait like sports). Both refused a clean fit, and for
related but distinct reasons: TBPN's pack appears to trade in a batch near
the show's end rather than reacting live to each mention; Rogan's pack
appears to run on priors (common words priced days in advance) and
text-metadata scanning (a word in the video description reprices the
market within 2 minutes of publish, with zero audio ever needed) rather
than genuinely listening. **Neither result resembles WNT/MLB/WNBA's
clean, slope≈1.0, single-anchor-model joins.** This is a real structural
difference between "broadcast/sports" mention markets (one clock: the
moment of utterance) and "podcast" mention markets (multiple, faster,
non-audio channels already competing) — worth treating as a genuine
finding, not a failed experiment: an audio-only detector, however fast,
structurally cannot beat a description-scanner on a podcast market, no
matter how much latency engineering goes into the audio path.

## Open items, not resolved here

- MLB stream URL extraction (browser network trace needed — the SoCast SRM
  player and TuneIn's resolver both load the stream dynamically).
- Pooling MLB/WNBA pack-lag across more games before trusting the offset
  as a population estimate, not a one-off.
- The `KXFTNMENTION` Monday-vs-Sunday discrepancy.
- WNBA per-team radio-station search (only 2/13 teams found this pass).
- The unresolved SPY-market fast-reaction mechanism (not base-rate, not
  description text — genuinely unknown).
- Whether TBPN's end-of-show batching and Rogan's description-scanning
  generalize to other podcast-format mention markets, or are specific to
  these two — untested on a third podcast venue.
- A live-vantage (US vs. non-US) geo test for any sports feed — none of the
  candidate URLs found this pass were confirmed simulcast-identity-verified
  or geo-tested, per the WNT-mistake-learned discipline this investigation
  otherwise followed throughout.
