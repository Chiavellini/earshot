# MLB commentator feed accessibility

## 1. Rights structure — confirmed distinct from TV, and favorable

Live web research (2026-08-05) confirms MLB's audio and video rights are
structurally separate products with different blackout regimes:

- **"MLB live audio for all 30 teams has no blackouts."** MLB+ (the current
  2026 branding, successor to the old At Bat audio app) sells this
  specifically: **$59.99/year or $5.99/month**, live audio of all 30 teams,
  no blackouts — audio is explicitly carved out from MLB.TV's video blackout
  rules. ([MLB+ Package](https://www.mlb.com/live-stream-games/subscribe/mlbplus),
  [MLB+ 2026 season hub](https://mlb.com/news/mlb-plus-2026-season))
- **Local team radio broadcasts are separately called out as blackout-immune**:
  "local radio broadcasts are immune to blackouts" — distinct from local TV
  (RSN) blackouts, which are real and geography-keyed.
  ([fansided 2026 blackout guide](https://fansided.com/mlb/how-watch-every-mlb-team-regular-season-games-2026-tv-schedule-streaming-info-blackout-rules))
- **International audio purchase confirmed**: MLB+ audio is available to
  international customers; the "U.S. only" restriction found in research
  applies to *MLB Network* streaming specifically, not the audio product.
  ([MLB.TV intl](https://www.mlb.com/live-stream-games/subscribe/intl))

This directly supports the plan's hypothesis: MLB's radio/audio rights are
licensed on a materially different, looser basis than TV — not swept into
the same blackout regime the WNT/OTT reasoning was built around.

## 2. Stream discovery — one concrete, free, ungated example found

**KNBR 680/104.5 (San Francisco)** — the flagship radio home of the SF
Giants (MLB), 49ers (NFL), and Warriors (NBA) simultaneously. Confirmed via
multiple independent sources to be freely streamable, globally, with no
paywall:
- **TuneIn**: "TuneIn.com offers KNBR 680 across the globe for free."
- Also listed free on Online Radio Box, Streema, AllRadio.Net.
- Station's own page: `https://www.thesportsleader.com/listenlive/`.

**Direct stream URL — NOT resolved in this pass, flagged honestly rather
than guessed.** I tried two paths:
- TuneIn's public `Tune.ashx` resolver (`opml.radiotime.com/Tune.ashx?id=s34231&render=json`)
  returned a response, but pointed at a placeholder
  (`.../notcompatible.enUS.mp3`) rather than a working stream — the station
  ID extracted from the public TuneIn URL isn't the right internal preset ID
  for this resolver; needs a browser network trace to get the real ID.
- The station's own listen-live page runs on **SoCast SRM**
  (`cdn-*.socastsrm.com`), a third-party radio-streaming platform — the
  actual audio endpoint is loaded dynamically by JavaScript, not present in
  static HTML. Resolving it needs a browser session (network tab inspection)
  rather than a plain HTTP fetch.

**MLB+ itself**: likely delivered through the MLB App / MLB.com player
(HLS-based, per general MLB streaming architecture), not a bare public URL —
whether it's a fetchable HTTP/HLS stream or app/DRM-locked (the SiriusXM
failure mode already on file for WNT) is **unverified** in this pass; it
would require an actual paid subscription to test, which is out of scope
for a research-only session.

## 3. Decodability test — NOT run

No confirmed, resolvable stream URL was obtained in this pass (see above),
so `scripts/venue-probe.sh` was not run against a real MLB audio candidate.
This is the concrete next step once a browser-session URL trace or an MLB+
subscription resolves an actual endpoint.

## 4. Simulcast verification — not applicable yet

No stream was tested, so nothing to verify. For when a URL is resolved: the
plan's cross-check method (compare what's said/what's happening against the
MLB Stats API play-by-play feed at the same wall-clock moment — see
`pm-expansion-exploration/latency/mlb/` pack-lag work) is the right method,
reusing infrastructure another part of this investigation is already
building.

## 5. Geo/blackout test — not run (no resolved URL to test from multiple vantages)

## Bottom line

**MLB's radio rights structure is genuinely favorable** — audio is
confirmed blackout-free (both local-radio and the paid MLB+ national audio
product), internationally purchasable, and at least one flagship station
(KNBR) is confirmed free and globally accessible by multiple independent
aggregator sources. What's still open is the purely mechanical step of
resolving an actual fetchable stream URL — both paths tried (TuneIn's
resolver, the station's own SoCast-powered player) need a browser network
trace to finish, not further desk research. This is a "confirmed
structurally open, mechanically unresolved" verdict, not a blocker.
