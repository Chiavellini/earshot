# LIVE RUN — Wednesday 2026-07-15, Morgan Stanley Q2 call (CURRENT)

> The JPM section below is retained as the original template; the JPM call
> (2026-07-14) passed without a live run — its markets are finalized. The MS
> run below was fully drilled the night of 2026-07-14 (all drills PASSED:
> Twilio hold probe, kill-drill 140 ms, mention probes on TARI/RECE, dry-run
> arm 14/14 markets, loopback rehearsal with 3/3 keyword detections, and an
> end-to-end `launch-event.sh` dress rehearsal against the real MS bridge).

Posture: **mention-fill, tiered** (`--i-am-live --allow-mention --fillable`) —
recession (cap 26¢) and tariff (cap 45¢; live ask was 38¢ on 07-14 evening,
the generated 14¢ cap could never fill) may cross the ask and FILL; the other
12 words observe at 1¢ non-crossing. Worst case 83¢, backstopped by the 100¢
event notional cap and the per-event $1 ledger (fresh:
`state/KXEARNINGSMENTIONMS-26JUL15/probe_ledger.json` = 0¢).

- **Call**: Wednesday 2026-07-15 **8:30 AM ET = 6:30 AM CDMX local**
  (results ~7:30 AM ET = 5:30 AM local).
- **Dial-in** (primary source morganstanley.com IR, verified 2026-07-14):
  **+1-844-886-2200** (intl +1-646-307-1096), passcode **400700#**.
- **Markets**: `KXEARNINGSMENTIONMS-26JUL15` — 14 active (verified via API
  2026-07-14); rules stored `state/KXEARNINGSMENTIONMS-26JUL15/rules/`.
- **Account env**: `~/.earshot/ms.env` (Twilio + Kalshi combined, created
  2026-07-14, mode 600).

## Morning checklist — ⚠ call is **6:30 AM local (CDMX, UTC-6)** = 8:30 ET

This box runs America/Mexico_City (UTC-6, no DST). Operator arrives 6:20 →
**10-minute window**. ONE command does everything (cleanup → sanity → launch
→ dial); it was dress-rehearsed end-to-end the night before:

```
cd ~/Code/ear && scripts/ms-morning.sh        # at 6:20:00 sharp
```

Timeline: ~6:20 launch → arm validates 14 markets + books (~40 s) → tunnel +
dial → "stream started" by ~6:22 → bridge hold until the 6:30 call start.
The bridge IVR disconnects silent callers after ~60 s pre-conference
(measured); if that happens pre-6:25, the script's dial can be re-run —
`scripts/twilio-dial.sh` line is printed on exit. Do NOT dial before ~6:15.

Kill (either leg, any time):
- `touch state/KXEARNINGSMENTIONMS-26JUL15/STOP`  → orders: cancel + disarm
- `scripts/twilio-hangup.sh <CallSid printed at dial>` → audio leg
Report after the call: `state/KXEARNINGSMENTIONMS-26JUL15/report.json`.

If the DTMF passcode isn't accepted (bridge prompt timing), hang up and
re-dial manually with more leading waits: `--dtmf "wwww400700#"` (each w =
0.5 s). The dress rehearsal confirmed the bridge answers and streams audio;
passcode acceptance can only be proven when the conference is open.

### Keyword rehearsal results (TTS through real Twilio, night of 07-14)

All 14 words produced ≥1 Confirmed detection via the live detector settings
(fp32, chunk 80 ms). Detect latency 64–127 ms past capture. Caveats to
expect live:
- **tariff** (fillable, cap 45¢): fired on mid-sentence "tariff"/"tariffs";
  MISSED sentence-initial "Tariff…" after silence, twice. Matches the known
  onset weakness (corpus recall 0.90). A missed first mention may be caught
  on a later mention — budget stays armed until filled.
- **tailwind** (observe 1¢): 2/3 phrasings fired; weakest of the 14.
- **recession** (fillable, cap 26¢): fired cleanly on TTS; still has zero
  corpus positives — live recall unproven.
- middle east/iran, oil (2nd mention), tailwind (2nd) correctly suppressed
  by refractory/budget after the first hit.

---

# First live run — Tuesday 2026-07-14 (JPMorgan Q2 call)

Posture: **mention-probe** — 1¢ IOC × 1 contract per keyword, $1 cumulative
ledger, Mentions category permitted via `--allow-mention`. The goal is a
timing + executionability verification, not P&L. Monday 07-13 is the full
dress-rehearsal day (no Kalshi mention calls exist that day — verified
against the open-events API 2026-07-11).

## Call targets (verified 2026-07-11 from issuer press releases + Kalshi API)

All five banks publish **static dial-ins, no registration**:

| Rank | Call (ET) | Kalshi event | Dial-in | Passcode |
|---|---|---|---|---|
| PRIMARY | JPMorgan 8:30 AM | `KXEARNINGSMENTIONJPM-26JUL14` (18 mkts) | 888-324-3618 / +1-312-470-7119 | `1364784#` |
| backup | Goldman 9:30 AM | `KXEARNINGSMENTIONGS-26JUL14` (13) | 1-800-330-6730 | `7042022` |
| backup | Wells Fargo 10:00 AM | `KXEARNINGSMENTIONWFC-26JUL14` (16) | 1-888-673-9782 | `8320644#` |
| backup | Citi 11:00 AM | `KXEARNINGSMENTIONC-26JUL14` (14) | 1-805-309-2350 | `7142026#` |
| skip | BofA 8:30 AM (conflicts with JPM) | `KXEARNINGSMENTIONBAC-26JUL14` (16) | 1-877-200-4456 | id `79795` |

Re-verify the JPM number on jpmorganchase.com IR Monday (sourced from a
syndicated reprint). A same-morning relay (JPM→GS→WFC→C) is possible later;
first run is JPM only. `configs/twilio-live.toml` carries the JPM tickers
(TARI corpus-tuned, RECE corpus-FA-tuned, INFL engine defaults).

## Prerequisites (user actions, do Saturday — external latency)

1. **Twilio**: create account, UPGRADE from trial (trial injects a voice
   preamble and restricts destinations), buy a voice-capable number, enable
   US outbound dialing. Put creds in `~/.twilio/earshot_env`:
   `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`.
2. **ngrok**: install; ideally reserve a stable domain (free hostnames
   rotate per session).
3. **Fee schedule**: fetch https://kalshi.com/docs/kalshi-fee-schedule.pdf
   in a real browser (bot-blocked), diff vs VERIFICATIONS §Q5 Feb-2026
   formula. Blocks trusting the EV gate live.

## Sunday drills (in order, each blocks the next; ~$0.03 total worst case)

Check ledger headroom first: `cat state/probe_ledger.json` (cap 100¢).

```bash
source ~/.kalshi/earshot_env
cargo build --release

# 1. Kill switch still <1 s, unrelated orders untouched (non-mention ticker)
cargo run --release -- kill-drill --ticker <liquid KXBTCD strike> --auto

# 2. Ack shape unchanged (1¢ IOC, non-mention)
cargo run --release -- probe-order --ticker <same>

# 3. NEW: mention-market order acceptance at $0 (refuses if 1¢ would cross)
cargo run --release -- probe-order --ticker KXEARNINGSMENTIONJPM-26JUL14-TARI --allow-mention

# 4. Dry-run arm against the real JPM markets: validator stores rules
cargo run --release -- arm --config configs/twilio-live.toml --allow-mention
#    → READ state/rules/KXEARNINGSMENTIONJPM-*.json and fix variants in the
#      config if rules_primary differs from ["tariff","tariffs"] etc.
#      Exclude any "said N times" strike (one-shot machine can't trade it).

# 5. Twilio loopback rehearsal (docs/TWILIO.md §Loopback):
#    ngrok http 8080 → dial a <Play>-fixture TwiML Bin → 3 tone detections,
#    measured capture latency, raw JSONL captured; then repeat with sherpa
#    + spoken keywords; finally --i-am-live (probe, non-mention 1¢).
```

## Tuesday timeline (ET)

- **T−60 m (7:30)**: `cargo build --release`; NTP sane (arm enforces ±10 s);
  start ngrok, note host; `book --ticker KXEARNINGSMENTIONJPM-26JUL14-TARI
  --secs 30` (live book sanity, note asks); confirm config allowlist =
  exactly the armed tickers.
- **T−20 m (8:10)**: `rm -f state/STOP`; then
  `cargo run --release -- arm --config configs/twilio-live.toml --i-am-live --allow-mention`
  (validator → gate summary → WS snapshots → presigner → executor idle).
- **T−15 m**: `scripts/twilio-dial.sh --to +18883243618 --dtmf "1364784#"
  --stream-url wss://<ngrok-host>/media` — hold music should start flowing:
  watch frame logs, seq-gap warnings, drop counters.
- **Live from the start.** The 1¢ × 1 posture IS the safety margin, and
  mention words can land in the CFO's first sentence. Only if the feed looks
  unhealthy at T−5 (drops climbing, gaps, silence) restart the call leg or
  fall back to a shadow session.

> **Two events at once (relay/parallel calls):** this timeline is single-event.
> To run two simultaneous calls on two separate Kalshi + Twilio accounts, use
> `scripts/launch-event.sh` once per event (each with its own config, account
> env file, `--bind-port`, and `--state-dir state/<id>`) instead of the manual
> arm/dial above — see `docs/MULTI-EVENT.md` §8 for the full setup + checklist.

## Event-prep + tiered fill (auto-config, M3+)

Instead of hand-cloning a config, generate it from the Kalshi event:

```
earshot prepare --event-ticker KXEARNINGSMENTION<ISSUER>-<YYMMMDD> --fillable
  # → configs/twilio-live-<issuer>.toml. TIERED: only precision-vetted words
  #   (tariff, recession — the tuned_library) fill at the live ask; every other
  #   word is 1¢ observe (logs fires, spends nothing, can't starve the $1 ledger).
  # Review: set [event].starts_at, research the dial-in, sanity-check variants.
```

Live: `arm --config <cfg> --i-am-live --allow-mention --fillable`. Fillable
orders cross the ask up to `MENTION_FILL_CAP_CENTS` (50¢), bounded by the config
notional caps + the $1 probe ledger. `--fill-all` makes EVERY word fillable —
deliberate only (false-alarm + ledger-starvation risk on unvalidated words).

Post-call, promote unvalidated words WITHOUT recording (respects "do not record"
below): `scripts/validate-from-log.py --log <arm.log> --config <cfg> --transcript
<public-transcript>` → per-word precision/recall from the observe-mode fire log.
A word that fires cleanly (~0 false alarms) graduates into `prepare::tuned_library`
and auto-fills next call. The rigorous path (record → corpus v2 → `bench-detector`)
is the durable follow-on for the recall ceiling.

## Kill criteria — any one → `touch state/STOP` then `scripts/twilio-hangup.sh <CallSid>`

- an order fires on a word you did not hear;
- frame drops / Twilio seq gaps climbing, or `feed_age` refusals appear;
- any refusal you don't understand;
- ledger at/near cap.

## The thesis measurement (capture during + after the call)

1. Note wall-clock time when you HEAR each keyword (your ear, rough).
2. Detection log line timestamp (tracing) + `report.json` (all 11 edges).
3. After the call: fetch each strike's actual close/settle time via REST —
   `close_time`/settlement on `GET /markets/{ticker}` — Kalshi humans close
   the market on utterance. **our-ack-time vs market-close-time is the
   measured edge**; positive margin = the strategy works.
4. `cargo run --release -- orders` + fills reconciliation; pin any new wire
   facts (mention-market ack on IOC, close behavior) in VERIFICATIONS.md
   with the evidence; write the run log.
5. Do NOT record call audio anywhere.

## Post-run

Verdict on: capture health (drops/gaps), detection correctness (per ear),
order acceptance (ack shapes), timing edge (ack vs close). THEN the M4 caps
conversation, with data.
