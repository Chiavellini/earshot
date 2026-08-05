# Pass B — Kalshi widescan beyond `category=Mentions`

Live, read-only sweep of `GET https://api.elections.kalshi.com/trade-api/v2/series`
(2026-08-05), reusing `scripts/mentions-census.py`'s API base/UA convention.
One call with `limit=200` returned the **entire** catalog with no cursor
(12,494 series total — the `limit` param does not appear to actually cap
this endpoint's response), so this scan is exhaustive, not sampled.

## 1. Category breakdown (live, confirms the taxonomy — not documented reliably elsewhere in this repo)

| Category | Series |
|---|---:|
| Sports | 3,126 |
| Entertainment | 2,495 |
| Politics | 2,133 |
| Elections | 1,535 |
| Financials | 749 |
| Economics | 618 |
| **Mentions** | **403** |
| Climate and Weather | 291 |
| Science and Technology | 283 |
| Crypto | 271 |
| Companies | 173 |
| World | 143 |
| Health | 96 |
| Commodities | 77 |
| Social | 52 |
| Transportation | 38 |
| Exotics | 10 |
| Education | 1 |

(`Mentions` here = 403, vs. `slate/census.json`'s 398 from 2026-07-30 —
5 more series appeared in the 6 days since that baseline was taken; a fresh
`mentions-census.py --classify` run would pick these up normally, not a
widescan-specific finding.)

## 2. Title-regex scan across all 12,091 non-Mentions series

Patterns: `\bsay(s)?\b`, `\bmention(s|ed)?\b`, `\bword(s)?\b`,
`during (his|her|their|the)`, `\bspeech\b`, `\bstated?\b`, `\bannounc`.
**161 raw hits.** The large majority are noise from generic English usage of
"state" (US states, Secretary of State races, "State of the Union," state
legislatures — ~90 hits) and "announce" (product launches, IPOs, tour
announcements, executive departures, election-run announcements — ~55 hits)
that have nothing to do with detecting a word spoken live during an event.

**Genuinely relevant hits — 7, all variations on classes already known, not a new venue type:**

| Ticker | Category | Title | Relationship to existing coverage |
|---|---|---|---|
| `KXMMMENTION` | Entertainment | "What will the announcers say during the March Madness game?" | Sports-announcer mention market **miscategorized outside `Mentions`** — same class as the already-PARKED `KXMLBMENTION`/`KXWNBAMENTION`/etc. family (linear-TV blocker), just filed under Entertainment instead of Sports or Mentions. |
| `KXSBMENTION` | Sports | "SB mention" (Super Bowl) | Same sports-mention class, same likely blocker. Miscategorized under Sports instead of Mentions. |
| `KXOSCARMENTION` | Entertainment | "Oscar mentions" | New within-cluster item — award-show mention market, same shape as `KXCMAMENTION`/Espys already in `candidate-longlist.md` cluster E. Almost certainly same linear-broadcast blocker as other award shows. |
| `KXWHBRIEFING` | Politics | "WH Briefing room people mentioned" | Same class as `KXSECPRESSMENTION` (already Tier 2 in EXPANSION.md) — a multi-person "who gets mentioned" board rather than a single-word board. Worth noting as a *different market shape* on an already-known venue, not a new venue. |
| `KXWHBRIEFINGEY` | Politics | "Eliezer Yudkowsky mentioned" | Same WH-briefing venue as above, single-person instance. |
| `KXVANCEINGRAHAM` | Politics | "What will J.D. Vance say on Ingraham Angle tonight?" | A named-guest-on-a-show instance — same underlying venue as `KXINGRAHAMMENTION` (cluster B in candidate-longlist.md), just a one-off guest-specific board rather than the recurring show board. |
| `KXCOMMENTSWIFTRUMP` / `KXSTATEMENTCOUNTSWIFTTRUMP` | Social / Entertainment | "Will Taylor Swift mention Donald Trump before October/September 15?" | **Different shape, flag don't survey as a live-event race**: long time-window (weeks), resolves on ANY public statement (interview, social post, etc.) over the period, not a single live event with a fixed audio stream. Same caution already flagged for `KXTRUMPSAY*` in candidate-longlist.md — re-verify `rules_primary` before assuming this is audio-detectable at all. |

## 3. Clear negative result

**No wholly new venue class was found outside `category=Mentions`.** Kalshi's
own categorization is doing its job for this market type — the 403 series
already in `category=Mentions` appear to be a near-complete accounting of
"will X say/mention Y" markets on this platform. The widescan's value is
narrow but real: it surfaced 2 markets that would silently evade
`mentions-census.py`'s existing sweep (`KXMMMENTION`, `KXSBMENTION` — both
miscategorized sports-mention boards, already covered by the standing
sports-blocker verdict) and one genuinely new-shaped board
(`KXWHBRIEFING`/`KXWHBRIEFINGEY`, a multi-person mention board on an
already-known venue). Recommend `mentions-census.py` stay `category=Mentions`
-scoped going forward — a full 12k-series title scan for this little
incremental yield is not worth running routinely.
