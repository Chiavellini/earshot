# Venue survey — Pass D (checklist depth on the top of candidate-longlist.md)

Scope: 4 candidates surveyed in depth (prioritizing cluster A government
remarks and cluster C podcasts, per the longlist's own recommendation),
plus 7 more checked for live Kalshi state only. Two parallel forks are
covering Pass B (Kalshi all-categories widescan) and Pass C (Polymarket
widescan) — this pass does not duplicate that discovery work; see their
own output files.

## Surveyed in depth

### Zohran Mamdani (NYC Mayor) — `KXMAMDANIMENTION`
**Live right now**: `KXMAMDANIMENTION-26AUG05`, 14 markets, resolves on his
*next* announcement (close_time 2026-08-27 is a backstop deadline, not a
call time — this is open-ended/ad-hoc, not a scheduled event). Sample
words (live-pulled `rules_primary`): AI/Artificial Intelligence,
Trump/President, Economy/Economic, NYPD, Grocery, Affordability.
Confirmed: NYC Mayor's Office press conferences ARE regularly posted to
YouTube — 5+ examples found across Jan–Jul 2026 (consumer protection,
education, housing, small business, extreme heat). **Not confirmed**: a
single canonical live channel, or whether these post live vs. after the
fact (matters a lot for latency — needs a follow-up check before
capturing). **Blocker-free** but genuinely ad-hoc — capturing this
requires a monitoring/discovery mechanism for the *next* presser, not a
pre-plannable window like FOMC.

### Fed governors beyond the chair — `KXFEDGOVMENTION`/`KXWALLERMENTION`/`KXWARSHMENTION`/`KXMIRANMENTION`
All 4 live-checked: **zero open events right now** — real, dormant series
that only open a board when a specific speech is scheduled. Confirmed
speeches DO get streamed, but each one's video lives on a *different*
third-party host's channel (a Waller speech at the Brookings Institution
used Brookings' own stream, not a Fed-owned feed) — a per-event discovery
job, not a one-time pin, structurally like the Trump-at-various-events
(G7/CPAC/RGA) cluster in the longlist. **Incidental finding worth flagging
to whoever owns the FOMC/`KXFEDMENTION` lane**: search results reference
Kevin Warsh as Fed *chair* and Stephen Miran's resignation around
2026-05-14 — if a leadership change happened, `KXFEDMENTION`'s existing
Powell-centric recon may be stale. Not verified further here; out of this
pass's scope.

### The Joe Rogan Experience — `KXROGANMENTION`
Currently dormant on Kalshi (0 open events), but **Polymarket already has
this class live** — the Task-1 Gamma pull found "What will be said on the
first Joe Rogan Experience episode of the week? (August 3)" already
listed, even while Kalshi's mirror is closed. Stream: single, confirmed,
long-established official YouTube channel (PowerfulJRE, 20.9M subs) — the
lowest `source_identity_risk` of anything surveyed this pass, since there's
no ambiguity about which channel is authoritative (unlike WNT's
wrong-24/7-channel failure). Cadence: recurring but irregular, order of
~150-250 episodes/yr (not verified precisely).

### TBPN (Technology Business Programming Network) — `KXTBPNMENTION`
Currently dormant on Kalshi. **Structurally the most FOMC-like candidate
found this pass**: fixed daily live window (weekdays 11am–2pm PT),
confirmed single official channel (@TBPNLive), ~250 episodes/yr, ~70k
viewers/episode, recently OpenAI-acquired (2026) so plausibly rising
profile. Guests have included Zuckerberg, Altman, Cuban, Nadella — high
name-recognition audience, business/tech vocabulary likely to overlap
words already validated readiness-wise on adjacent earnings-call boards.

## Checked for existence only (not deep-surveyed — time-boxed out of this pass)

`KXTALARICOMENTION`, `KXBONDIMENTION`, `KXHEGSETHMENTION`, `KXRUBIOMENTION`,
`KXSCOTUSMENTION`, `KXCONGRESSMENTION`, `KXPORTNOYMENTION` — all live-
confirmed as real series, all currently **dormant (0 open events)**. Worth
re-checking periodically (these open opportunistically, not on a fixed
schedule) rather than assumed permanently closed. Do not treat these as
surveyed beyond existence/dormancy — no stream/cadence/blocker research was
done on them this pass.

## Cross-cutting takeaway

The two candidates with the LEAST operational friction (TBPN, Rogan) are
both cluster-C podcasts with fixed/confirmed single channels — exactly the
prediction the longlist made before this pass ran. The government-remarks
cluster (A) is real and blocker-free but trades that for per-event
discovery cost: no fixed channel, no fixed schedule, a board only exists
once a speech is announced. Mamdani is the one live, actionable exception
right now, but even it is ad-hoc (resolves on "next announcement," not a
dated event) — someone would need to notice the announcement is happening,
same operational shape as the Trump-remarks class already flagged in
`docs/EXPANSION.md`.
