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
