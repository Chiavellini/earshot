# WNBA commentator feed accessibility

## 1. Rights structure — much thinner than MLB's, and a real geo-inversion finding

Live web research (2026-08-05) found WNBA does **not** have MLB's mature,
separately-licensed, blackout-free local-radio-audio product structure.
Local radio flagship stations were only clearly identifiable for a couple of
teams (Indiana Fever: WFNI; Washington Mystics: Monumental Sports Network) —
suggesting WNBA local radio presence is genuinely thin/inconsistent across
the league, not a reliable per-team feed the way MLB's is. **Do not assume
every WNBA team has an equivalent local radio call** — this needs a
per-team check before building anything, not a league-wide assumption.

**The real finding here is about WNBA League Pass (video), not radio, but
it's the clearest geo-inversion evidence found in this whole investigation:**
"In countries where WNBA League Pass is available, **all games are
available live, except in the US and Canada**, where local and national
blackouts apply." ([WNBA League Pass FAQ](https://www.wnba.com/webview/leaguepass/faq))
— i.e., the blackout regime is explicitly and structurally **US/Canada-only**;
a non-US vantage (the project's existing CDMX default) would face **zero**
WNBA blackout restrictions where a US-based box would face real ones. This
is the plan's "blackout/geo-inversion hypothesis" confirmed directly by the
league's own stated policy, not inferred — though note it's confirmed for
the **video** product (League Pass), not a dedicated audio-only product,
since no such WNBA-specific audio product was found (see below).

## 2. Stream discovery

- No WNBA-equivalent of "MLB+" (a standalone, blackout-free, audio-only
  subscription covering all teams) was found. WNBA audio appears bundled
  into League Pass (primarily a video product) or into national radio
  coverage.
- **National coverage exists via SiriusXM** ("ESPN Xtra," channel 81) for
  play-by-play — this is the SAME distribution mechanism already measured
  and found slow (145-155s end-to-end, Wowza) for the unrelated WNT/ABC News
  lane (`docs/COMPETITOR-ANATOMY.md`). Do not assume SiriusXM's WNBA feed is
  faster; if this is pursued, it should be independently latency-tested, not
  assumed fast by analogy — the existing 145-155s number is for a different
  content stream on the same platform, not directly transferable, but it's a
  real reason for skepticism.
- Team-level: only the Fever (WFNI) and Mystics (Monumental Sports Network)
  turned up as clearly named flagship stations in this pass — worth checking
  each of the other ~11 WNBA teams individually before concluding the class
  is unusable, since this may be a search-coverage gap rather than a real
  absence.

## 3-5. Decodability / simulcast / geo tests — not run

No resolved stream candidate exists yet for WNBA (unlike MLB, where KNBR is
at least a confirmed-free, globally-accessible candidate even though its URL
wasn't mechanically resolved this pass). Nothing to test.

## Bottom line

**WNBA is structurally weaker than MLB for this purpose** — thin/inconsistent
local radio presence, no equivalent blackout-free audio product, and the one
national option (SiriusXM) shares its distribution platform with an already-
measured-slow feed on a different lane. The one strong finding —
League Pass's US/Canada-only blackout structure — is real and useful evidence
for the broader geo-inversion hypothesis, but it's a video product finding,
not a ready-made low-latency audio answer. **Recommend deprioritizing WNBA
relative to MLB** for the next concrete build step, and doing a per-team
radio-station check (not done here) before fully closing the door on it.
