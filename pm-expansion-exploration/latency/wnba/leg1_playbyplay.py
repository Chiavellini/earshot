#!/usr/bin/env python3
"""leg1_playbyplay.py — WNBA structured play-by-play as a pack-lag ground truth.

LEG-1 SOURCE DECISION (verified live 2026-08-05, don't trust this blindly on
a future run — re-check if it stops working):

  stats.nba.com (the API `nba_api` and most community tooling wraps, and
  which was hoped to also cover WNBA under the same infrastructure) is
  UNREACHABLE from this environment: TLS handshake completes but every
  request (with or without the community-standard
  x-nba-stats-origin/x-nba-stats-token/Referer/Origin headers) times out
  with zero bytes after 20-25s. This reads like a datacenter-IP block
  (consistent with this project's own prior findings on other services —
  YouTube's segment-layer bot-wall, `docs/COMPETITOR-ANATOMY.md`), not a
  transient failure. data.wnba.com (legacy JSON endpoint) returns a hard
  403 Access Denied from an Akamai edge. site.api.espn.com also 403s.

  **site.web.api.espn.com DOES work** (HTTP 200, confirmed live) and its
  `/apis/site/v2/sports/basketball/wnba/summary?event=<id>` endpoint
  returns a `plays[]` array where EVERY play carries a `wallclock` field —
  a real ISO-8601 UTC timestamp, not just a game-clock string. This is the
  leg-1 source this module uses. Event ids come from
  `/apis/site/v2/sports/basketball/wnba/scoreboard?dates=YYYYMMDD`.

TIMESTAMP-MEANING CAVEAT (flagged, not resolved — do not treat as verified):
whether a play's `wallclock` reflects the actual on-court instant or a
stat-recorder's data-entry lag is UNCONFIRMED by this module alone. What
IS confirmed, empirically, against Kalshi's own trade tape (see
pack_lag_wnba.py's real run): a slope-fit across 3 matched (play, Kalshi
sweep) pairs from 2 different games came out at slope=0.997 (tolerance
0.02) — i.e. whatever `wallclock` measures, it advances in lockstep with
the market's own reaction clock, which is the alignment check that matters
for this pattern's purpose even if the absolute few-second precision of
`wallclock` itself stays unverified.

MARKET-MATCHING CAVEAT, the more important one: KXWNBAMENTION strikes are
literally "What will the announcers SAY" markets (verified live via
`GET /markets?event_ticker=...` — every strike's title is an announcer
word/phrase, e.g. "Travel / Travels / Traveled", "Tech / Technical",
"Overtime", "Caitlin Clark", "MVP"). A structured play-by-play event is
only a valid ground-truth proxy for a strike when the WORD is said
reflexively, in direct reaction to that specific play type — confirmed
true for "Traveling" and "Technical Foul" turnover/foul plays (announcers
call these out immediately, near-universally). It is NOT reliably true in
general: the LVCHI 2026-08-01 game's "Overtime" strike settled YES even
though ESPN's play-by-play shows the game ending after period 4 (no
period-5/OT plays at all) — the word was evidently said for some other
reason (a hypothetical, a reference to a different game, pre-game
discussion), not because the game actually went to overtime. Do not assume
a strike's name maps cleanly onto a play-by-play event type without
checking; this module only trusts "Traveling" and "Technical Foul" turnover
plays as anchors, and even those should be re-verified per-game rather
than assumed permanently reliable.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

ESPN_BASE = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/wnba"
UA = "Mozilla/5.0"

# Kalshi strike-code -> ESPN play `type.text` values trusted as a reflexive,
# same-instant proxy for the word being said. Extend cautiously; verify any
# new mapping against a real settled market before trusting it (see the
# Overtime counter-example above).
TRUSTED_STRIKE_TO_PLAY_TYPES = {
    "TRAV": ("Traveling",),
    "TECH": ("Technical Foul",),
}


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def scoreboard(date_yyyymmdd: str) -> list[dict]:
    """[{id, shortName, date}, ...] for one date (ESPN's own event ids)."""
    d = _get(f"{ESPN_BASE}/scoreboard?dates={date_yyyymmdd}")
    return [
        {"id": e["id"], "shortName": e.get("shortName"), "date": e.get("date")}
        for e in d.get("events", [])
    ]


def play_by_play(event_id: str) -> dict:
    """Full ESPN summary payload for one game (contains plays[], each with
    a `wallclock` ISO-8601 UTC field, per-play `type.text`, `period.number`)."""
    return _get(f"{ESPN_BASE}/summary?event={event_id}")


def tipoff_utc(pbp: dict) -> datetime:
    """First play's wallclock, treated as the local t0 for this game."""
    plays = pbp.get("plays", [])
    if not plays:
        raise ValueError("no plays in payload")
    return datetime.fromisoformat(plays[0]["wallclock"].replace("Z", "+00:00"))


def trusted_anchors(pbp: dict, strike_code: str) -> list[datetime]:
    """Wallclock timestamps of every play matching a TRUSTED strike-code's
    play types, for one game. Empty list if the strike code isn't in the
    trusted map or the event type never occurred in this game — both are
    real, valid outcomes, not errors."""
    types = TRUSTED_STRIKE_TO_PLAY_TYPES.get(strike_code)
    if not types:
        return []
    out = []
    for p in pbp.get("plays", []):
        if p.get("type", {}).get("text") in types and p.get("wallclock"):
            out.append(datetime.fromisoformat(p["wallclock"].replace("Z", "+00:00")))
    return out


def self_test():
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        if want == got:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("leg1_playbyplay self-test (synthetic, no network):")

    fake_pbp = {
        "plays": [
            {"wallclock": "2026-08-01T19:03:56Z", "type": {"text": "Jumpball"},
             "period": {"number": 1}},
            {"wallclock": "2026-08-01T19:35:18Z", "type": {"text": "Technical Foul"},
             "period": {"number": 2}},
            {"wallclock": "2026-08-01T20:24:13Z", "type": {"text": "Traveling"},
             "period": {"number": 4}},
            {"wallclock": "2026-08-01T20:30:00Z", "type": {"text": "End Game"},
             "period": {"number": 4}},
        ]
    }

    chk("tipoff is the first play's wallclock",
        datetime(2026, 8, 1, 19, 3, 56, tzinfo=timezone.utc), tipoff_utc(fake_pbp))

    tech_anchors = trusted_anchors(fake_pbp, "TECH")
    chk("TECH anchors find the technical foul play", 1, len(tech_anchors))
    chk("TECH anchor timestamp is correct",
        datetime(2026, 8, 1, 19, 35, 18, tzinfo=timezone.utc), tech_anchors[0])

    trav_anchors = trusted_anchors(fake_pbp, "TRAV")
    chk("TRAV anchors find the traveling play", 1, len(trav_anchors))

    chk("untrusted strike code returns empty, not an error", [],
        trusted_anchors(fake_pbp, "MVP"))

    chk("a trusted code with zero occurrences in this game returns empty",
        [], trusted_anchors({"plays": fake_pbp["plays"][:1]}, "TECH"))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
