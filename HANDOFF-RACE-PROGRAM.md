# HANDOFF-RACE-PROGRAM.md

**Handoff for the EARSHOT webcast-race program. Written 2026-07-23 evening,
after the first live fill day. Read this before touching anything; then read
`CLAUDE.md` → `docs/ARCHITECTURE.md` → the layer doc for whatever you change.**

---

## 1. What this system is and where it stands

EARSHOT detects spoken keywords in live earnings-call audio and races Kalshi
"mention" markets. The 07-22→23 sprint proved, live and with real money, that
every mechanical component works: multi-leg audio acquisition, streaming
detection, cross-leg dedupe, risk-gated crossing IOC execution from us-east —
**decision-to-matched-trade in 9.9 ms**. The system lost ~$0.26 today and is
one detector-quality iteration away from being what it promises. The race
deficit against the fastest live players is **~1 second of software we own**,
down from −8.7 s two days ago.

### Headline results (2026-07-23, all live)

| Event | Posture | Result |
|---|---|---|
| LMT 8:30 ET (autonomous, probe, CDMX laptop arm) | dry-run fanout | Δrace GOLD **−0.6 s**, MORF **−1.0 s**, EURO **−2.8 s** (vs −8.7 s baseline 07-22); NASA settled **YES** — our true flag ignored by the market for 52 min; PERU + IRAN settled **NO** (our false fires) |
| AAL 8:30 ET (secondary capture only) | box capture | ladder-compression replicated; 8/15 words swept (rich offline Δw set) |
| INTC 5:00 PM ET (fill test, **arm on us-east box**) | `--i-am-live --allow-mention --fillable`, fill-all ≤50¢, $1 envelope | **FIRST DETECTION-TRIGGERED MENTION FILL EVER**: APPL 1×YES @26¢ (≈false fire, −26¢); CLI raced **−1.02 s**; no-pump guard refused TRUM + CLI correctly; ledger closed 28¢/100¢ |

Evidence: `docs/COMPETITOR-ANATOMY.md` §Per-tape (LMT/AAL/INTC 07-23 entries),
`VERIFICATIONS.md` §Q-FILL, `state/KXEARNINGSMENTION{LMT,AAL,INTC}-26JUL23/`
(reports, tapes, box capture reports), box `~/intc-live.log`.

---

## 2. What is PROVEN (do not re-litigate; cite these)

| Capability | Proof | Evidence |
|---|---|---|
| Fanout multi-leg detection (N legs, own detector each, OR'd at ONE one-shot `DecisionTable` behind `Arc<Mutex>`) | 3 live events, 0 drops, 100% cross-leg dedupe (16/16 LMT, 129/129×2 INTC) | `src/pipeline/mod.rs::run_pipeline_fanout`, `tests/e2e_fanout.rs`, reports' `per_leg` |
| Edge-join acquisition legs (streamlink `--hls-live-edge 1` + ffmpeg `-live_start_index -1`) | measured at 3 real venues | `configs/webcast-fanout-arm.toml` (template), COMPETITOR-ANATOMY per-tape |
| **Order path at colo-class speed** | APPL: decision 21:20:38.999689Z → tape print 21:20:39.009549Z = **9.9 ms**; `send→hdrs` 17.7 ms us-east→Kalshi | Q-FILL; `state/KXEARNINGSMENTIONINTC-26JUL23/report-box.json` |
| Frozen-cap "no-pump" guard | refused TRUM (ask 19¢ > cap 11¢) and CLI (34¢ > 27¢) — zero cents chased | box `~/intc-live.log` LOST_RACE lines |
| $1 envelope | 7 orders: 4 observe-never-sent, 2 refused, 1 sent+filled; ledger 28¢/100¢ | report-box.json `orders[]`, box `state/.../probe_ledger.json` |
| Autonomous ops chain (registration → browser m3u8 surfacing → box capture → arm launch → wind-down → tape scoring) | LMT ran start-to-finish with the operator absent | RUNBOOK §Thu 07-23 G3 staging + rehearsal notes |
| us-east box arm (creds, WS books, executor) | INTC ran entirely on the box | box `~/.kalshi/` (creds), `~/intc-live.log` |
| Internal pipeline latency | `capture→decision` p50 **118 ms** / p95 210 ms | report-box.json `edges` |
| Market-missed-word channel exists | NASA (LMT): true flag at 12:38:33Z, market ignored until 13:30:24Z (52 min at ~20¢), settled YES | LMT tape + COMPETITOR-ANATOMY LMT entry |

**Execution is not a suspect in any loss. Do not spend time on order-path
latency.** (Fire-path/OS work already measured; see `docs/LATENCY-PROOF.md`.)

---

## 3. What is BINDING (the exact defects, with evidence)

1. **Detector precision — the #1 money leak.** Fill-tier flags today: 3, of
   which 2 were false ("apple" filled −26¢; "trump" refused only because the
   market had drifted). Two-day fill-tier/word precision ≈ 4/6 (LMT) and 1/3
   (INTC). sherpa exposes **no per-hit score** (`confidence` constant 1.0 —
   `min_confidence_*` gates are INERT on this engine); the real knobs are
   per-keyword `fast_threshold`/`confirmed_threshold`/`boost` + variant
   engineering, tuned on a corpus. A two-leg AND-gate would NOT have saved
   APPL (both legs heard it, 2.2 s apart).
2. **Detector recall.** INTC: 4 swept words never flagged — ARIZ (arizona),
   DESK (desktop), GOVE (government), MOBI (mobileye) — plus ULTR's first
   utterance missed (flagged a repeat 8 min late). LMT: ORIO + VENT missed.
   Misses skew compound/OOV, but GOLD ("golden dome") WAS caught — it is
   tunable, not hopeless.
3. **Decode lookahead.** sherpa's chunk-16 export = 320 ms baked lookahead +
   decode ⇒ our flag trails word-end by more than wave-1's (~200–400 ms class,
   per MS anatomy). This is the largest identified share of the remaining
   race gap. (Note: `chunk_ms` is NOT a lever — refuted, `docs/BAKEOFF.md`.)
4. **Frozen-cap forfeit (policy, not bug).** CLI: at our arrival the book
   showed a real 34¢×200 ask; someone took it 90 ms after we refused (cap
   27¢); it settled 99¢. The freeze is why we never chase pumps — but it also
   forfeits +EV late fills. Any "chase band" must be priced against the fact
   it would ALSO have bought TRUM at 19¢ (a false fire). Quantify on tape data
   before changing.

---

## 4. Why the race is judged WINNABLE (the decomposition)

- This venue class (Notified→CloudFront, 4 s segments) runs **~10 s behind
  the room** (PDT edge lag, `manifest-probe.txt` in each capture dir). If
  wave-1 listened on the private analyst phone bridge they would lead any
  webcast consumer by ~10 s. **They lead by 0.6–2.8 s ⇒ they are co-listeners
  on the same segmented feed**, winning on software. No structural moat
  observed in any of 6 contested races across 2 days.
- Our internals are 0.14 s. The ~1 s deficit = decode lookahead (~0.3–0.5 s)
  + segment-acquisition timing (who parses the word-bearing segment first;
  up to ~0.5 s+ within a 4 s-segment world) + jitter.
- **Fetch flags are exhausted at short-window venues**: the reference-stream
  ladder (−11.7 s streamlink) COMPRESSES to −1.2 s (tuned) / −0.2 s
  (streamlink) at real Notified venues, and the leg order REVERSES (tuned
  ffmpeg beat streamlink 5/6 first-arrivals). Measured twice same-day
  (LMT + AAL box captures, xcorr peaks 0.9997). Do not tune fetch flags
  further for this venue class; DO manifest-probe every new venue (long-window
  venues would re-activate the ladder).

---

## 5. Prioritized work queue (in order; each ends with a measured verdict per the standing convention)

**P1 — Detector quality on the banked corpus (top priority; gates all fill
posture).**
- Corpus: LMT WAVs `box:~/lmt-capture/*.wav` (+ `dead-run-1/` archive), AAL
  `box:~/aal-capture/*.wav` (~94 min × 3 legs each). **INTC audio was LOST**
  (capture had 0 frames — pre-fix config); recover it from the Notified
  replay: `edge.media-server.com/mmc/p/rdz8ppwz` (available through
  **2026-08-06**; registration identity below). LMT/AAL replays same pattern.
- Ground truth: sweep timestamps already scored in COMPETITOR-ANATOMY; get
  transcripts (`scripts/transcript-fetch.py`, `section-label.py`) for
  word-level truth (= checklist item W5).
- Tune: per-keyword thresholds/boost + variants via `bench-detector`
  (`docs/BAKEOFF.md` protocol; `corpus-process.sh`/`corpus-align.py` chain).
  Targets with names: APPL/TRUM/PERU/IRAN false-fire class; ARIZ/DESK/GOVE/
  MOBI/VENT/ORIO/SAFE/TARA miss class.
- Pre-register a promotion gate before FOMC (e.g. dev precision ≥ X on the
  false-fire set with recall not degraded).

**P2 — Decode latency (the race lever).**
- P1-checklist: re-export sherpa at chunk-16→**chunk-8/160 ms** quantum
  (`scripts/export-english-streaming-ctc.md` has export notes; RACE-CHECKLIST
  P1). G2b: fair Parakeet-Realtime-EOU-120M re-bench (shortlist pinned in
  RACE-CHECKLIST G2a). Bench on the NEW wideband corpus, paced, A/B vs
  current sherpa per `docs/BAKEOFF.md`.
- Prefix/Fast-tier on compounds: prefix-fire was refuted for phone vocab
  (~32 ms), but multi-word webcast phrases ("client computing", "golden
  dome") have long decisive prefixes — re-evaluate ONLY for compound-phrase
  keywords, on the corpus.

**P3 — Segment-acquisition experiments (bounded; venue-dependent).**
- Test at a live reference venue: chunked-transfer fetch of the in-progress
  segment; 48k vs 128k audio-variant race; manifest-poll phase-locking to
  segment cadence. Only worth it if P2 lands; measure with the capture
  harness xcorr (`earshot capture-bakeoff`).

**P4 — Chase-band policy (design + backtest only; NO live change).**
- Backtest on the two days' tapes: for each cap policy (frozen, frozen+N¢,
  live-ask≤50¢), replay our actual flags → P&L. The CLI forfeit vs TRUM save
  is the exact tradeoff. Output: a table in RACE-STRATEGY; user decides.

**G4 — FOMC Wed 2026-07-29 2:30 PM ET (the crown target).**
- Box arm (validated posture), `manifest-probe.sh` the venue FIRST (unknown
  platform — if long-window, fetch ladder matters again; if LL-HLS, W3 opens).
- Posture: user-gated. Standing rule from 07-22: fill only if the P1 precision
  gate is green; else probe. Pre-declared bar: steady-state Δrace ≥ 0 s,
  no drain artifact, fanout recall ≥ single-leg.
- Friday 07-24 AXP/VZ 8:30 AM ET: staging was queued on the 5:43 PM cron
  (probesize-fixed templates already synced to box); if that cron died with
  the session, stage manually per §6.

---

## 6. Operational map (everything has a place)

**Repo/branch:** `webcast-lane` (NOT pushed; merge decisions pending). All of
this sprint is committed. Key new files: `src/pipeline/mod.rs` (fanout),
`tests/e2e_fanout.rs`, `configs/webcast-fanout-arm.toml` +
`scripts/stage-webcast-arm.sh` (armed template), `scripts/launch-webcast.sh`
(`--wait-live` gate; REFUSES fill flags by design — fill runs use direct
`earshot arm` invocation, see Q-FILL/RUNBOOK), `scripts/stage-webcast-capture.sh`
(capture kits), fill configs `configs/{twilio-live,webcast}-intc*-fill.toml`.

**us-east box:** `ubuntu@44.197.147.18`, key `~/.ssh/earshot-useast1.pem`
(instance i-04c50d76572aa092e; no scheduled shutdown — it runs until stopped;
user pays while it runs). On the box: repo at `~/ear` (rsync'd, NOT a git
clone), binary `~/ear/target/release/earshot` (fanout tree), models present,
streamlink 8.4.0, **Kalshi creds at `~/.kalshi/{earshot_env,earshot.pem}`**,
capture WAVs at `~/{lmt,aal,intc}-capture/`, arm log `~/intc-live.log`,
one-shot launcher `~/launch-intc-fill.sh` (pattern for future fill launches).

**Laptop (CDMX):** creds `~/.kalshi/earshot_env` (+`earshot.pem`), Twilio
`~/.twilio/earshot_env`, mailer `~/.kalshi/mail_env`, per-event account envs
`~/.earshot/{aal,intc,lmt,ms}.env`. Pinned binaries + per-event state under
`state/KXEARNINGSMENTION<EV>/` (ledger is **per state-dir**, 100¢ hard cap,
debits on SEND not fill, never refunded).

**Webcast registrations (live sessions in Chrome, keep-me-logged-in):**
identity **`bdelriom@actinver.mx` / Bernardo Del Rio / Actinver** on all three
Notified players — LMT `mmc/p/k8rv9ajf`, AAL `mmc/p/jkbexuu7` (phone field:
used the Twilio MX number), INTC `mmc/p/rdz8ppwz` (its `authentication_
required` guestbook ACCEPTED this identity — that risk is retired). The
webcast is listen-only; the analyst Q&A bridge is private (IR emails asking
for dial-ins were drafted 07-23; check `bdelriom@actinver.mx` for replies —
a reply with digits creates a phone leg that beats the webcast by ~10 s).

**Morning surfacing recipe (proven 3×):** open logged-in player tab → call
`read_network_requests` FIRST (tracking starts on first call) → reload →
the live line appears as `metafile.pro1.mci…` + a
`d2qqe54r01064t.cloudfront.net/<hash>/<id>/h264-aac-audio-en/playlist.m3u8`.
Validate: EXTINF present, **NO `EXT-X-ENDLIST`**, MEDIA-SEQUENCE advancing
(the pre-event hold clip is a 15-min VOD with ENDLIST — arming against it
kills the legs at its end). Media URLs are UNAUTHENTICATED (verified 3
venues) — write the URL to `~/<lc>-stream.url` on laptop AND box; the box
fetches CloudFront directly. Timing: LMT's line rose 27 min early, INTC's
~8 min early — poll from T−40 min.

**Session-bound things that died with the old session:** all crons (Friday
staging + any reminders). Recreate what you need; recipes in RUNBOOK.

---

## 7. Footguns (every one of these cost us something — do not rediscover them)

1. **probesize × bitrate:** Notified live audio is 77 kbps. ffmpeg with
   `-probesize 1000000` emits NOTHING for ~100 s ⇒ every leg dies on the 30 s
   pipe idle guard. Use `-probesize 65536 -analyzeduration 1000000` (now in
   the templates). This killed the LMT arm's first launch AND all three
   morning captures AND (unfixed config) the entire INTC capture.
2. **ssh-piped `tail -f` buffers without a tty** — a Monitor on it goes
   silent while everything works. Use `ssh -tt`. Cost: one blind hour mid-INTC.
3. **CRLF manifests** (DW/Akamai): a variant URL with trailing `\r` makes
   curl throw "malformed URL"; strip with `tr -d '\r'` (fixed in
   launch-webcast.sh).
4. **`curl | grep -q` under `set -o pipefail`**: grep's early exit SIGPIPEs
   curl, pipeline rc≠0, the `&& break` silently never fires. Capture body
   first, then grep (fixed in launch-webcast.sh).
5. **STOP-then-SIGTERM:** the kill watcher is ONE-SHOT. After a STOP-kill,
   SIGTERM does not terminate the arm. For endless streams, wind down by
   killing the LEG SUBPROCESSES (`pkill -x ffmpeg`, `pkill -f '[s]treamlink'`
   — note the bracket trick: a plain `pkill -f streamlink` over ssh matches
   the ssh command itself and kills your session) → sources EOF → clean
   report. Watcher fix is an open item.
6. **Never score Δrace from a tape tail-pull.** A `limit=100` newest-trades
   read caught the END of GOLD's sweep and produced a false "+11.7 s ahead"
   mid-call. Full tape (`scripts/fetch-tape.sh`) only.
7. **`capture-bakeoff` holds frames in memory and writes WAVs only at
   end-of-run.** An empty record-dir mid-run is NORMAL; check process-level
   health only; killing it loses everything (GOOGL 07-22, 8 min of tape).
8. **launch-webcast.sh refuses fill flags BY DESIGN.** Don't "fix" that;
   fill posture = documented direct `earshot arm --i-am-live --allow-mention
   --fillable` invocation.
9. **`prepare` output needs hand-fixes:** `starts_at` is a placeholder; the
   dial-in is a TODO comment; regenerate `--fillable` configs close to the
   call so frozen caps reflect fresh asks.
10. **Kalshi transient network failures at arm-boot happen** (one balance
    self-test failure at LMT); probe `exchange/status` and relaunch — it
    cleared in 2 min.
11. **Executor fire-path logs are sparse by design** (FIRE FIRST): a sent
    order may show NO log line near the intent. The ledger debit and the
    report's `orders[]` (`order_status: "filled"`) are the truth; the public
    tape print at your decision timestamp is the independent confirmation.

---

## 8. Conventions and gates that bind you

- **CLAUDE.md is law**: layer docs must match code in the same change; wiring
  in exactly two places; money = integer cents; Kalshi wire facts go in
  `VERIFICATIONS.md` dated with evidence; kill switch scope never widens.
- **Measured-verdict discipline** (user's standing requirement): latency/
  detection work ends with interleaved A/B or side-by-side numbers and
  explicit not-resolvable verdicts. The laptop has ~3.5× run variance — bench
  on the box.
- **Risk envelope (all still in force):** `MENTION_FILL_CAP_CENTS = 50`
  (price bound), `PROBE_LEDGER_CAP_CENTS = 100` per event state-dir, event
  notional config 100¢. Widening any of these is **M4-gated** and needs an
  explicit user decision recorded in the docs (user declined a $5 raise on
  07-23 — respect that precedent; ask, don't assume).
- **Fill posture is user-gated per event.** The 07-23 fill test was a one-off
  authorization. Default posture is probe/observe until the P1 precision gate
  is green AND the user says go.
- **Unattended paths count as validated only when rehearsed with the exact
  production mechanism** (the reason the 07-23 morning worked at all).

## 9. First hour for the next agent

1. Read `CLAUDE.md`, this file, `docs/RACE-CHECKLIST.md` (live gate status),
   `docs/COMPETITOR-ANATOMY.md` §Per-tape 07-23 entries, `VERIFICATIONS.md`
   §Q-FILL.
2. Check APPL/TRUM final settlements (expected NO/NO) and pin the realized
   P&L (−26¢) in COMPETITOR-ANATOMY.
3. Confirm Friday AXP/VZ staging happened (box kits + arm configs + preflight);
   if the 5:43 cron died, stage per §6 recipes.
4. Verify the box is still wanted running (it bills while up) and the three
   player sessions are alive.
5. Start P1 (corpus). The Notified replay window for INTC closes **2026-08-06**.
