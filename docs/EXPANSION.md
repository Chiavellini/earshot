# EXPANSION — where this infra is reusable, ranked

`as_of: 2026-07-30` · `authority: expansion-triage` · owner node: discovery

Evidence base: `slate/census.json` (full 398-series Mentions census, 0 survey
errors, 2026-07-30), VERIFICATIONS.md **Q-CENSUS / Q-PM / Q-UNOPENED /
Q-ADMIT**, `docs/COMPETITOR-ANATOMY.md` (venue-class ledgers), live Polymarket
sweep 2026-07-30. Verdicts use the lane kill/continue pattern
(COMPETITOR-ANATOMY §lane verdict): **CONTINUE** (act now), **PROBE**
(cheap measurement first), **PARKED(blocker)** (viable, named blocker),
**KILL** (measured dead / structurally unwinnable), **NOTE** (different infra,
recorded only).

Ranking principle (user directive 2026-07-30): **the phone lane is the
lowest-latency feed we have (10–30 s ahead of webcast, INGRESS-BAKEOFF §0);
expansion prioritizes markets reachable by a dial-in bridge.** Everything else
competes on the webcast/broadcast floor where our measured H1 position is
contestable-but-thin (median Δrace −1.28 s, n=4).

## What a new event class must supply (contract)

1. **Ingress** — either (a) a PSTN number that survives admission
   (Q-ADMIT Class A: automated passcode bridge; Class B operator screens need
   `[operator_handler]` + rehearsal), or (b) any subprocess emitting s16le
   mono PCM on stdout (`SourceConfig::Pipe` — HLS/DASH/YouTube/anything;
   SPEC P1: a new feed is a new adapter, not a new pipeline).
2. **Keywords** — a Kalshi board whose `rules_primary` compiles to variants
   (`earshot prepare`), passing the blocking readiness gate
   (`variant-study.sh`; dead-on-TTS words don't fire live — BAKEOFF §RECALL).
3. **Registry** — `configs/dial-ins.toml` is keyed by ISSUER ticker; non-issuer
   classes (Fed, hearings, TV) have **no registry schema yet** — a schema
   extension is a prerequisite for any Tier-2 phone hunt.

---

## Tier 1 — CONTINUE (phone-lane class; infra reusable end-to-end today)

### 1. Deepen Kalshi earnings-mention coverage ★ highest priority
**Surface:** 58 earnings series live (51 in-prefix + 7 in the
`KXMENTIONEARN*` second family — NOT dead, Q-CENSUS), **48 open events right
now**, listed 1–2 weeks ahead. ~85–90 % of S&P 500 issuers run a real bridge;
only ~35–40 % publish a way in (DIAL-INS.md n=41); our probed `held` set is
single-digit.
**Latency position:** phone bridge = the venue class where we hold the
fastest measured ingress; Δ phone-vs-webcast +4.1–8.2 s measured (CMG 07-29).
**Action:** (a) W1 fixes make discovery census-complete; (b) W2 slate-stage
automates config pairs; (c) quiet-day `bridge-probe-batch.sh` sweep over the
48 open issuers' numbers → grow `admission="held"` coverage. Metric: held-count
before/after; every new `held` issuer is a Tier-1 event forever after.

### 2. Polymarket dual-listing of the SAME events (measure-only)
**Surface:** PM tag `mention-markets` mirrors the class at 10–100× volume
(Powell pressers $0.9–5.5M, NFLX earnings $6.6M) and lists the *same*
AAPL/AMZN 26JUL30 calls (keyword overlap ≈11/18 on AAPL). Single-mention
markets = our exact race; cumulative-count markets ("say AWS 20+ times")
re-price on every utterance — a streaming counter holds edge the whole call.
**State:** read APIs fully public (Q-PM); `scripts/pm-capture.py` live on the
box since 2026-07-30 15:49Z capturing AAPL+AMZN books+trades.
**Action:** capture during every armed Kalshi event → `pm-edge-report.py`
after ≥3 overlaps → provable-improvements verdict. **Execution (wallet, CLOB
orders, Polygon) stays gated on that report — user decision 2026-07-30.**

---

## Tier 2 — PROBE (webcast floor; H1 measured contestable; no phone lane known)

### 3. FOMC / Fed-chair pressers (`KXFEDMENTION`, 43-market boards) — the crown
Venue known (Brightcove/boltdns/Fastly), PM mirror is the largest mention
volume anywhere. Greeting strikes are pure races. Already program-of-record;
expansion adds nothing new — keep.

### 4. Congressional hearings (`KXHEARINGMENTION`, 2 open events NOW)
Committee webcast streams; venue mapping per hearing (RUNBOOK week-of-07-20
recon precedent). Latency class unmeasured. **PROBE:** capture one hearing's
stream, measure edge-lag + wave-1 presence per the H1/H2/H3 discriminator.

### 5. Debates (`KXDEBATEMENTION` — 2 events pre-open for 26AUG03, found via
`status=unopened` — Q-UNOPENED's poster child)
TV/stream venue TBD; word boards are political vocab (readiness-friendly). Same-day
listing risk high — exactly what the W1 intraday watcher closes. **PROBE** on
26AUG03.

### 6. Recurring speech/appearance classes (White-House briefings
`KXSECPRESSMENTION`/Leavitt + PM mirror, Trump events, `KXECBMENTION`)
Recurring supply, stream venues, PM cross-listing ($29K+ briefings). Crowd
noise / crosstalk = precision risk (WNT autopsy class). **PROBE** with a
capture-only session before any arm.

### 7. Podcast / YouTube-live classes (`KXALLINMENTION` weekly, PM All-In)
YouTube live = same-floor for everyone, but us-east-1 bot-walls the segment
layer (pinned) and CDMX default-join was the TSLA lesson. **PROBE:** one
capture-only session measuring YT-live edge lag vs market sweep.

## Tier 3 — PARKED (viable market, blocked on a linear-TV feed)

### 8. `KXWORLDNEWSMENTION` (daily ABC newscast) — PARKED(linear feed, no
unblock path available)
14 first-utterance strikes/day × ~250 sessions/yr = the highest-leverage race
surface found (COMPETITOR-ANATOMY §broadcast). Blocked: market requires the
linear ABC network feed; RF/AM/FM closed 4 ways; OTT 45 s–2 min behind.
**STANDING 2026-07-30: no US physical presence exists or is planned — the
ATSC/antenna and US-IP live-TV-subscription rungs are closed; do not cost
them out.** The lane re-opens only if a linear-lag feed appears that needs no
US presence. Detector-accuracy work continues on the VOD corpus (Q-WNT-VOD).

### 9. Sports announcer mentions (`KXMLBMENTION` ×6 open, `KXWNBAMENTION` ×4,
CFB/NFL/NBA/NHL/EPL/UEFA family — ~20 daily-cadence series) — PARKED(same
blocker, same closed rungs)
Continuous daily supply, but regional-sports linear feeds have the same
OTT-lag wall, and the same no-US-presence rule closes the hardware answers.
Count strikes ("Trump 5+ times") are prediction problems, not races — NOTE
for a future non-latency book.

## KILL / NOTE

- **RF/AM/FM radio ingress** — KILLED four ways (pinned; do not revisit).
- **Text lanes** (PM NYT-front-page, Trump/Elon posts; Kalshi `KXTRUMPSAY*`
  weekly/monthly cumulative boards) — different infra entirely (no audio;
  scraping + classification). NOTE-only; Ford post-mortem already proved
  text-lane reality is its own program.
- **`KXEARNINGSMENTIONSYUM`** and other typo series — zero events; census
  tracks them; never special-case by hand.

## Census mechanics + known gaps

- `scripts/mentions-census.py` (Q-CENSUS): full-category sweep, delta alerts
  `NEW-FAMILY:` / `OUT-OF-PREFIX-OPEN:`; baseline committed at
  `slate/census.json`. Wire-up cadence: weekly inside slate-daily (W1.3).
- Gap: Kalshi nulls `volume`/`open_interest` on nested market objects
  (verified 2026-07-30), so census liquidity columns read 0 — per-target
  liquidity for shortlists must come from `/markets/{ticker}` or the public
  trades endpoint at triage time.
- Gap: title-heuristic classification leaves ~211/398 unclassified (series
  titles are terse); classify against the latest EVENT title in a future
  census pass. Does not affect delta alerting.
