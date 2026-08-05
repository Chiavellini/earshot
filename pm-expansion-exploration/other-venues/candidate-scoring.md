# Candidate scoring — Task 2

## Method

Hard gates first (mirrors `docs/EXPANSION.md`'s existing KILL/PARKED
taxonomy rather than inventing a new one) — any of these ends scoring
immediately:
- `linear-TV-only` with no unblock path (the standing no-US-physical-
  presence constraint: RF/ATSC/cable and US-IP live-TV subs are all
  closed rungs).
- Stream probe fails with no workaround.
- No live market exists at all on either venue (a hypothesis with no
  tradeable instrument behind it).

Then a weighted score, 0-5 scale:

```
score = 0.30*liquidity + 0.20*frequency + 0.30*feasibility + 0.20*(5 - competitive_intensity)
```

- **liquidity** (0.30): Kalshi + PM combined volume/OI potential. PM's raw
  Gamma volume is a *weak* proxy — Task 1 measured PM's own claimed
  10-100x edge overstated the earnings class by 27x once same-window
  coverage was applied — so this is triage-grade, not a final verdict.
- **frequency** (0.20): annualized opportunity count, bucketed (≥250/yr=5
  ... ad-hoc/unknown=1). Separate from liquidity on purpose — a thin
  single-event edge at very high frequency (WNT's "14 strikes/day ×
  250 sessions/yr") was called the highest-leverage race surface found
  before its blocker killed it; frequency alone doesn't save a hard-gated
  venue, but it matters a lot among survivors.
- **feasibility** (0.30): stream-probe result, minus a crosstalk-risk
  penalty, plus a bonus for a confirmed single official channel (source-
  identity risk = low) and for a fixed/predictable schedule.
- **competitive intensity, inverted** (0.20): no measured Δrace/notional-
  ratio evidence exists yet for any candidate below — default neutral
  (2.5) and flagged `assumed-needs-live-probe` rather than guessed.

## Sanity check against EXPANSION.md's existing verdicts

Before trusting this rubric on new candidates, run it on the already-
decided ones:
- **`KXWORLDNEWSMENTION` (WNT)** — hard gate: `linear-TV-only`, no unblock
  path (confirmed still true — this is the standing constraint, unchanged).
  **Auto-PARKED.** Matches EXPANSION.md's existing Tier 3 #8 verdict. ✓
- **Sports family** (`KXMLBMENTION` etc.) — same hard gate, same result.
  **Auto-PARKED.** Matches Tier 3 #9. ✓
- **`KXFEDMENTION`** — no hard gate (stream known, Brightcove/boltdns/
  Fastly, already probed). Liquidity 5 (PM's largest mention volume
  anywhere per EXPANSION.md), frequency 2 (~8/yr, scheduled-far-ahead so
  feasibility gets the schedule-predictability bonus), feasibility 4
  (venue known, already recon'd, greeting-strike races proven), inverted-
  competitive 2.5 (unmeasured, neutral default). Score ≈ 0.30(5) +
  0.20(2) + 0.30(4) + 0.20(2.5) = 1.5+0.4+1.2+0.5 = **3.6/5 — high,
  matches EXPANSION.md's "the crown" framing.** ✓

Rubric agrees with prior measured findings on all three checks — proceed.

## Scored: this pass's surveyed candidates

| venue_id | hard gate | liquidity | frequency | feasibility | comp (inv) | score | verdict |
|---|---|---:|---:|---:|---:|---:|---|
| `tbpn` | none | 2 (unmeasured, business/tech niche, PM not checked) | 5 (~250/yr, fixed daily slot) | 5 (confirmed single channel, fixed schedule, no crosstalk beyond 2 co-hosts) | 2.5 (unmeasured) | **3.35** | **PROBE — highest-feasibility new candidate found** |
| `joe-rogan-experience` | none | 3 (PM already lists this class live, confirmed real demand) | 3 (~150-250/yr but irregular) | 4 (confirmed single channel, but long-form 2-3h episodes and irregular guest/topic reveal cut prep time) | 2.5 (unmeasured) | **3.15** | **PROBE** |
| `mamdani-nyc-mayor-announcements` | none | 2 (unmeasured; PM not checked; NYC-local, likely thin vs. federal-level figures) | 2 (ad-hoc, no fixed schedule, board resolves on "next announcement") | 3 (blocker-free content-wise, but needs an announcement-discovery mechanism; live-vs-posted-after-fact unconfirmed) | 2.5 (unmeasured) | **2.35** | **PROBE, lower priority — real and live right now, but structurally like the already-flagged Trump-remarks ad-hoc class** |
| `fed-governors-non-powell` | none | 3 (Fed-adjacent, plausibly meaningful PM/Kalshi interest by analogy to KXFEDMENTION, unmeasured directly) | 2 (announced ad-hoc, not calendared) | 2 (per-event third-party-host discovery burden — a new venue to find every time, no stable channel) | 2.5 (unmeasured) | **2.35** | **PROBE, lower priority — real but high discovery overhead per event** |

## Not yet scoreable

`KXTALARICOMENTION`, `KXBONDIMENTION`, `KXHEGSETHMENTION`, `KXRUBIOMENTION`,
`KXSCOTUSMENTION`, `KXCONGRESSMENTION`, `KXPORTNOYMENTION` — confirmed real
and currently dormant, but not deep-surveyed (no stream/cadence/blocker
data) — cannot score responsibly yet. The ~190 remaining longlist entries
across clusters B/D/E/F/G are entirely unscored — this pass covered depth
on ~4 of ~206 non-earnings candidates, per the design's own sequencing
("cast the net first, cheap; deep checklist work only on a shortlist").

## Update after Pass B (Kalshi widescan) and Pass C (Polymarket widescan) landed

Both are read in full: `pass-b-kalshi-widescan.md`, `pass-c-pm-widescan.md`.

**Pass B: clean negative on new venue types.** All 12,494 Kalshi series
across every category scanned; the 7 genuinely relevant hits outside
`category=Mentions` are all variations on classes already known (sports-
announcer boards miscategorized under Entertainment/Sports, an award-show
board, a multi-person "who's mentioned" shape on the already-known WH-
briefing venue, a named-guest instance of an already-listed cable show).
Nothing here changes the scoring table — Kalshi's own `Mentions` category is
a near-complete accounting of this market type on that venue.

**Pass C surfaced one genuinely new, real-volume class and closed an open
question from its own findings.** Concretely pinned PM-side FOMC volume
(Powell pressers: $937K-$5.48M per meeting, closed-market history) —
strengthens `fed-mention`'s already-high standing but doesn't change
its score inputs (still unmeasured on THIS repo's own axes). More
importantly, Pass C found real recurring volume on **Senate confirmation
hearings** (Kevin Warsh, $15K-$311K) and **Trump bilateral press
conferences** (Netanyahu $168K, Carney $760K+$247K, Turkey $22K, plus
weekly/monthly Trump rollups $450K-$1.23M) and flagged as unresolved
whether these have Kalshi counterparts, since if not, they'd need new
(Polymarket-only) execution code rather than a capture-only config.

**Resolved here, directly against a live census pull**: they DO have Kalshi
counterparts. `KXWARSHMENTION`, `KXNETANYAHUMENTION`, `KXCARNEYMENTION`, and
`KXBILATERALMENTION` are all real series already sitting in
`slate/census.json`'s unclassified set (Pass A's cluster A/F) — currently
**0 open events** on the Kalshi side (dormant, like most of cluster A), but
the series exist. This is NOT a PM-only-market/new-execution-code situation
— it's the same "real series, opens opportunistically, needs someone to
notice" operational shape as the rest of cluster A, now with concrete PM-side
volume evidence that the opportunity is worth noticing when it opens.

| venue_id | hard gate | liquidity | frequency | feasibility | comp (inv) | score | verdict |
|---|---|---:|---:|---:|---:|---:|---|
| `warsh-senate-hearing` | none | 3 (PM $15K-$311K measured; Kalshi side unmeasured but confirmed to exist) | 1 (single/rare event — a confirmation hearing is not recurring for one person) | 2 (hearing webcast venue TBD, same discovery burden as `KXHEARINGMENTION`) | 2.5 (unmeasured) | **2.15** | **PROBE, event-specific — score when a Senate hearing is actually scheduled, not evergreen** |
| `trump-bilateral-pressers` | none | 4 (PM $22K-$1.23M measured, clearly real and recurring) | 4 (Pass C confirms Trump gets a fresh presser market for essentially every notable bilateral meeting — high frequency as a class even though each instance is one-off) | 2 (per-event venue discovery, same as cluster F in the longlist — White House press pool feed likely reusable across instances, unconfirmed) | 2.5 (unmeasured) | **3.0** | **PROBE — highest-liquidity new-this-pass candidate, but the operational shape (per-event discovery, no fixed channel) is the same overhead already flagged for cluster F/A** |

## Recommended next prototype

**TBPN** still wins (3.35 vs. `trump-bilateral-pressers`' 3.0) — highest
score, zero hard gate, lowest operational overhead (fixed schedule beats
ad-hoc discovery for a first proof point). See
`prototype-configs/tbpn-capture-sketch.toml`. Mirrors the WNT precedent's
*intent* (capture-only, no execution, no arm) while deliberately avoiding
its actual failure mode: TBPN's official channel is unambiguous and
independently confirmed by name (`@TBPNLive`), unlike WNT's wrong-24/7-
channel problem — but the source-identity gate should still run before
trusting any capture as representative, per house discipline.

**`trump-bilateral-pressers` is the one to watch as a close second**,
despite scoring lower: its liquidity is real and directly measured (not a
proxy), and unlike TBPN it already has a live, dormant Kalshi series
(`KXBILATERALMENTION` + person-specific variants) waiting for the next
bilateral meeting to be scheduled — meaning there's no new-market-listing
risk, only a discovery-timing problem. Worth a standing watch (e.g. folded
into `xvenue-daily.sh`'s health-check cadence: does `KXBILATERALMENTION`
have an open event today) rather than a dedicated prototype build right
now, since there's nothing to capture until one opens.
