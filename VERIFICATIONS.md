# VERIFICATIONS.md — resolved [VERIFY] items (meta-spec §12)

All findings dated **2026-07-10**, researched against live docs.kalshi.com (index:
https://docs.kalshi.com/llms.txt) and the live production API (read-only GETs).
Code must cite this file, not remembered API details. Format: Claim / Source /
Evidence / Implication / Confidence / Re-verify trigger.

---

## Q1 — Order-entry transports

**Claim:** Order entry is REST (plus FIX). The WebSocket API is market-data and
order/fill *notifications* only — it cannot submit orders.
**Source:** https://docs.kalshi.com/api-reference/orders/create-order-v2.md ·
https://docs.kalshi.com/websockets/user-orders.md
**Evidence:** Create Order V2 is `POST /portfolio/events/orders`; the
`user_orders` WS channel is "Real-time order created and updated notifications"
(server→client only); WS commands are limited to subscribe/unsubscribe/
`update_subscription`.
**Implication:** Hot path = HTTPS POST over warm keep-alive connections (or
FIX). Do not architect around WS order entry.
**Confidence:** high.
**Re-verify:** any changelog entry adding WS commands.

**Claim:** Build against the **V2** order shape now. The legacy
`/portfolio/orders` endpoint is deprecated "no earlier than May 6, 2026" (window
may already be open). V2 body: `ticker`, `side` = `"bid"|"ask"` **on the YES
leg**, `count` (string, ≤2 dp), `price` (string dollars, ≤6 dp),
`time_in_force` = `"fill_or_kill"|"good_till_canceled"|"immediate_or_cancel"`,
`self_trade_prevention_type`, optional `client_order_id`, `post_only`,
`cancel_order_on_pause`, `reduce_only`, `order_group_id`.
**Source:** create-order-v2.md (as above).
**Implication:** Prices are **dollar strings, not integer cents**; sides are
bid/ask-on-YES, not yes/no. IOC is a first-class `time_in_force`. Idempotency
via `client_order_id`.
**Confidence:** high. **Re-verify:** immediately before M2 implementation.

**Claim:** FIX order entry (FIXT.1.1 / FIX50SP2) is available with a FIX API
key: production `mm.fix.elections.kalshi.com:8228` (TargetCompID `KalshiNR`),
demo `fix.demo.kalshi.co`; market data `marketdata.fix.elections.kalshi.com:8233`.
Port 8230 (`KalshiRT` retransmission), Post Trade, and PrivateLink require
institutional approval (institutional@kalshi.com). FIX messages **drain the
same rate-limit buckets as REST** — no throughput advantage.
**Source:** https://docs.kalshi.com/fix/connectivity.md ·
https://docs.kalshi.com/fix/order-entry.md
**Implication:** FIX may shave per-request overhead but buys no rate-limit
headroom; defer until REST latency is measured (latprobe, M5 region work).
**Confidence:** high (endpoints); medium (no tier gating stated for basic FIX).
**Re-verify:** before any FIX go-live; access policy may be enforced out-of-band.

**Claim:** Batch endpoints exist (`POST /portfolio/events/orders/batched`,
batch cancel). A batch costs the same tokens as its parts (create = 10 tokens
each, cancel = 2) and the **whole batch must fit the bucket at once or the
entire batch is rejected**.
**Source:** batch-create-orders-v2 · getting_started/rate_limits.md
**Evidence:** "submitting 25 orders costs 25 × 10 = 250 tokens... A 25-order
create batch needs 250 tokens available when it arrives, or the entire batch is
rejected."
**Implication:** Batching saves round-trips and signatures (good for the
Powell burst case), not tokens. Size batches to `floor(bucket_balance / 10)`.
**Confidence:** high. **Re-verify:** on tier change or unexplained 429.

## Q2 — Rate limits

**Claim:** Tiers (reads/writes, tokens per second): Basic 200/100 · Advanced
300/300 · Expert 600/600 · Premier 1,000/1,000 · Paragon 2,000/2,000 · Prime
4,000/4,000 · Prestige 6,000/8,000. Most requests cost **10 tokens**; order
cancels cost **2**. Sustained rate = budget ÷ cost (Basic: 10 creates/s,
50 cancels/s). Authoritative per-endpoint costs: `GET /account/endpoint_costs`;
live budget: `GET /account/limits` (`refill_rate`, `bucket_capacity`).
**Source:** https://docs.kalshi.com/getting_started/rate_limits.md
**Confidence:** high. **Re-verify:** monthly, and on any modeled-vs-actual 429 mismatch.

**Claim:** Burst capacity: write buckets above Basic hold **2 seconds** of
budget (one burst of 2× per-second rate); Basic write buckets hold 1 second.
429 responses carry **no Retry-After / X-RateLimit headers** and no penalty.
**Source:** rate_limits.md (as above).
**Implication:** For an idle-then-burst mention bot, Advanced+ allows a
2-second write burst (Advanced: 60 orders at once). Retry after
~cost/refill_rate with local token-bucket mirroring — nothing to parse from 429s.
**Confidence:** high. **Re-verify:** if changelog adds rate headers.

**Claim:** **Advanced tier is free and self-serve** via the "Upgrade Account
API Usage Level" endpoint. Expert+ require 30-day trailing volume share
(Expert 0.075% earn / 0.05% keep, Premier 0.125%/0.10%, up to Prestige
1.00%/0.80%) or a support request with use-case description.
**Source:** rate_limits.md.
**Implication:** Call the upgrade endpoint on day one of M2 (100→300 w/s).
**Confidence:** high. **Re-verify:** monthly (thresholds float with exchange volume).

## Q3 — Request signing

**Claim:** Sign the UTF-8 string `{unix_ms}{METHOD}{path}` — where `path` is
the full path **from the API root including `/trade-api/v2`, excluding query
string** — with **RSA-PSS / SHA-256, salt length = digest length**, base64 the
signature. Headers: `KALSHI-ACCESS-KEY` (key id), `KALSHI-ACCESS-SIGNATURE`,
`KALSHI-ACCESS-TIMESTAMP` (ms).
**Source:** https://docs.kalshi.com/getting_started/quick_start_authenticated_requests.md ·
https://docs.kalshi.com/getting_started/api_keys.md
**Evidence:** Example message `1703123456789GET/trade-api/v2/portfolio/balance`;
"For .../portfolio/orders?limit=5, sign /trade-api/v2/portfolio/orders";
Python: `padding.PSS(mgf=MGF1(SHA256()), salt_length=DIGEST_LENGTH)`.
**Implication for pre-staging (spec §5.7):** the signature covers only
timestamp+method+path — **not the body** — so order bodies can be fully
pre-serialized; only the three auth headers need computing at fire time.
RSA-PSS signing costs ~100–500 µs: keep a warm signer, or pre-sign
short-lived headers on a refresh loop if skew tolerance allows.
**Confidence:** high. **Re-verify:** on any 401 with correct-looking headers.

**Claim (RESOLVED empirically):** The signing scheme above **works verbatim**:
`sign_probe` got 200 from live `GET /portfolio/balance` using RSA-PSS/SHA-256
(salt = digest length) over `{ms}GET/trade-api/v2/portfolio/balance` with the
three KALSHI-ACCESS-* headers.
**Source:** `examples/sign_probe.rs` run 2026-07-10 against production (read-only).
**Confidence:** high.

**Claim (RESOLVED empirically):** Timestamp skew tolerance is **±30 seconds**
(undocumented). Probe results (production, 2026-07-10): −28 s → 200, −30 s →
401 (the extra ~network transit tips it over), +30 s → 200, +35 s → 401.
Rejection is a plain 401 `authentication_error`.
**Implication:** Pre-signed headers (spec §5.7 pre-staging) are valid for
~30 s after signing. A signer refresh loop every ~10 s leaves 3× margin;
chrony keeps the box well inside the window. Alert on any 401 whose local
skew estimate is < 20 s — that means the scheme changed.
**Confidence:** high (measured). **Re-verify:** rerun `sign_probe` before M2
go-live and after any auth-related changelog entry.

## Q4 — Mention-market mechanics ⚠ (shapes the whole trade)

**Claim:** Mention markets **close and expire early, per strike, the moment
the word is spoken**. Verified live: `KXFEDMENTION-26JUL-VOLA` has
`can_close_early: true`, `early_close_condition: "This market will close and
expire early if the event occurs."`; `KXEARNINGSMENTIONDAL-26JUL10-YOUT`
(Delta earnings, today) closed intra-call at 20:08:40Z.
**Source:** `GET https://external-api.kalshi.com/trade-api/v2/markets?series_ticker=KXFEDMENTION`
(live production API) · https://kalshi.com/markets/kxfedmention/fed-mention
**Implication:** Confirms the spec's IOC-only rule — a resting order
post-utterance is pure risk. The bot must fill before Kalshi closes the strike.
Subscribe to `market_lifecycle` WS events to observe closes.
**Confidence:** high. **Re-verify:** per market at arm time (`early_close_condition`
is a per-market field).

**Claim:** Occurrence is determined by **Kalshi employees watching the live
video** (consensus), with official transcripts as fallback only.
**Source:** market `rules_secondary` (live API): "Video of the Jul 2026
post-FOMC meeting introductory remarks and Q+A will be primarily used to
resolve the market; if a consensus by Kalshi employees cannot be reached using
video, internal & external (official) transcripts... will be used."
**Implication:** The early-close race is against **human video watchers**
(seconds to tens of seconds after utterance), not an automated transcript
feed — this is the empirical upper bound on the edge window; measure it in the
M3 live shadow.
**Confidence:** high. **Re-verify:** per series (earnings series name different fallbacks).

**Claim:** What counts as a mention (NEWMENTION contract terms): the word as a
**distinct standalone unit**. Plurals and possessives **count**; tense/
conjugation/derivation changes do **not** ("Russia's" ✓, "Russian" ✗). Open and
hyphenated compounds count ("fire station" ✓, "pro-Palestine" ✓); closed
compounds do not ("wildfire" ✗). Homophones don't count; clear-intent
mispronunciations do; muted words don't absent corroboration. Merriam-Webster
AND OED adjudicate compound status.
**Source:** https://kalshi-public-docs.s3.amazonaws.com/contract_terms/FEDMENTION.pdf
**Implication:** The keyword `variants` compiler (spec §5.4) implements
exactly: plural/possessive expansion, word-boundary matching, no stemming.
Per-market `rules_primary` can override (multi-word phrases, "said X times",
OR-lists) — always re-download the series `contract_terms_url`.
**Confidence:** high (primary document). **Re-verify:** per new series traded.

**Claim:** Mention markets are a large surface: **379 series** in category
"Mentions" (Fed, earnings for DAL/NFLX/GS/DIS/ADBE/MU…, speeches, SNL, TV),
all sampled `fee_type: quadratic, fee_multiplier: 1`.
**Source:** `GET /trade-api/v2/series?category=Mentions` (live).
**Implication:** Series discovery is API-driven; standard taker fee applies.
**Confidence:** high. **Re-verify:** `GET /exchange/series-fee-changes` per series at arm.

## Q5 — Fee schedule

**Claim:** Taker fee = `ceil_cent(0.07 × C × P × (1−P))` per order; maker fee
(only on series flagged `quadratic_with_maker_fees`) = `ceil_cent(0.0175 × C ×
P × (1−P))`; INX/NASDAQ100 series use 0.035; **no settlement fee**; resting
orders and cancels are free ("Trading fees are only charged for orders that
are immediately matched"). No volume-tier fee discounts. Effective Feb 5, 2026.
**Source:** https://kalshi.com/docs/kalshi-fee-schedule.pdf (via Wayback
snapshot 2026-02-18: http://web.archive.org/web/20260218003606/...) —
corroborated by live API `fee_type: quadratic, fee_multiplier: 1` on all
sampled mention series.
**Implication (EV gate, spec §5.6):** worst case 1.75¢/contract at P=50¢;
at a 97¢ sweep price the fee is ~ceil(0.07×0.97×0.03) ≈ 0.3¢/contract — fees
are nearly negligible exactly where this strategy buys. Spec's "~7¢ × P×(1−P)"
formula confirmed.
**Confidence:** high for Feb 2026 edition; **medium for today** — a
"Fee Schedule for July 2026 - 7.7.26 Update" exists (Google-indexed) but
kalshi.com blocks non-browser fetches (Vercel checkpoint / 429).
**Re-verify:** ⚠ OPEN — fetch https://kalshi.com/docs/kalshi-fee-schedule.pdf
from a real browser and diff against the above before M2's EV gate is coded.

## Q6 — Demo/live parity

**Claim:** Base URLs — production REST `https://external-api.kalshi.com/trade-api/v2`,
production WS `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, demo REST
`https://external-api.demo.kalshi.co/trade-api/v2`, demo WS
`wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`. Demo uses the
**identical RSA-PSS signing scheme** (the official quick-start signs against
demo). WS handshake requires the same three auth headers. Legacy hostname
`api.elections.kalshi.com` still serves public market data (verified live).
**Source:** https://docs.kalshi.com/getting_started/api_environments.md ·
quick_start_authenticated_requests.md
**Confidence:** high. **Re-verify:** on connect failure (Kalshi has rotated hostnames before).

**Claim:** Orderbook stream: subscribe with `{"id":1,"cmd":"subscribe",
"params":{"channels":["orderbook_delta"],"market_ticker":"..."}}` (or
`market_tickers` array; auth required); server sends `orderbook_snapshot` then
`orderbook_delta`; `use_yes_price: true` reports all deltas in yes-leg pricing;
`update_subscription` supports add/delete markets and `get_snapshot` resync.
**Source:** https://docs.kalshi.com/websockets/websocket-connection.md ·
orderbook-updates.md
**Implication:** Use `use_yes_price` to eliminate a whole class of no-leg
conversion bugs in the M2 book cache.
**Confidence:** high. **Re-verify:** against https://docs.kalshi.com/asyncapi.yaml when coding M2.

**Claim:** The only documented demo gap is market realism ("price and behavior
of markets in the demo environment may not be reflective of those in real
markets"). No missing channels/endpoints documented.
**Implication:** Demo validates signing, order plumbing, WS handling — never
liquidity, queue dynamics, or early-close timing. Do not calibrate latency or
fill models on demo (spec §9.3 satisfied for plumbing parity only).
**Confidence:** medium (undocumented gaps likely, e.g. sparse mention markets on demo).
**Re-verify:** empirically on first demo connect — enumerate markets + channels.

**Claim (RESOLVED empirically, production):** WS auth + orderbook flow works
end-to-end with the same signed headers: handshake 101, `subscribed` ack, then
`orderbook_snapshot` for the subscribed ticker (probe run 2026-07-10,
read-only).
**Claim (empirical):** The API key on this box is **live-only** — demo
returned 401 `authentication_error / NOT_FOUND` for the same key. Demo has a
separate account/key registry.
**Implication:** Before M2's demo order round-trips, create demo credentials
at demo.kalshi.co and store them alongside `~/.kalshi/env` (e.g.
`~/.kalshi/demo_env`); `sign_probe demo` then validates them.
**Confidence:** high. **Re-verify:** n/a (action item).

---

## M2 live-drill pins (2026-07-10, production, bounded-loss 1¢ orders)

All discovered by real order round-trips; each cost ≤ $0.01.

**Claim:** `self_trade_prevention_type` is **REQUIRED** on V2 create-order
(docs list it optional; live returns 400 `missing_parameters` without it).
Use `taker_at_cross` for IOC, `maker` for post-only.
**Confidence:** high (pinned). **Re-verify:** on any create-order 400.

**Claim:** `count` must be a fixed-point 2-decimal string (`"1.00"`); plain
`"1"` → 400 `invalid_parameters`.
**Confidence:** high (pinned).

**Claim:** `client_order_id` must be **UUID-shaped**; arbitrary strings → 400
`invalid_parameters` (no field detail in the error). Earshot derives
deterministic UUIDs from `sha256("{event}:{kw}:{tier}")` so idempotency
survives the format constraint.
**Confidence:** high (pinned).

**Claim:** The 201 IOC ack has **no `order` wrapper and no `status` field**:
top-level `{order_id, client_order_id, fill_count, remaining_count,
average_fill_price, average_fee_paid, ts_ms}` (fp strings). Status must be
derived: `remaining==0 && fill>0` → filled; `fill==0` → nothing crossed.
**Evidence:** `{"average_fee_paid":"0.0007","average_fill_price":"0.0100",
"fill_count":"1.00","remaining_count":"0.00",…}`.
**Confidence:** high (pinned). Note the fee: $0.0007 on a 1¢ fill —
**centicent precision**, i.e. our ceil-to-cent EV model is conservative.

**Claim:** Legacy `DELETE /portfolio/orders/{id}` returns **410
deprecated_v1_order_endpoint**. V2 cancel is `DELETE
/portfolio/events/orders/{order_id}` → `{order_id, client_order_id,
reduced_by, ts_ms}`. Works (pinned by kill drill; cancel path 140 ms
STOP→cancelled from laptop).
**Confidence:** high (pinned).

**Claim:** WS orderbook wire format differs from the asyncapi docs:
snapshots carry `yes_dollars_fp`/`no_dollars_fp` arrays of
`[price_dollars, qty_fp]` strings; deltas carry `{price_dollars, delta_fp,
side}`. The documented `use_yes_price` subscribe param is **ignored** — each
side arrives in its own leg and the client must convert (YES ask = 100¢ − NO
bid). Also: a quiet market sends no frames for seconds — feed liveness must
be measured with pings, not message arrival.
**Confidence:** high (pinned live).

**Claim:** Measured order-path latency from this laptop: send→ack ~90–94 ms
(RTT-dominated), exec→send 352 µs with pre-signed headers + pre-staged body
(spec §3 stage-7 "<1 ms" target confirmed achievable). The ≤60 ms
detection→ack gate is judged from the M5 region, not from here.
**Confidence:** high (measured, n=2 sessions).

**⚠ Operational:** this Kalshi account carries **~52 resting orders from
other systems**. Any cancel logic must be scoped to the session's own order
ids (`OrderRegistry`) — an account-wide cancel-all would destroy unrelated
positions. This is now hard-coded policy in `rest.rs::cancel_orders`.

## M1 detector pins (2026-07-11)

**Claim:** The sherpa-onnx streaming KWS model
`sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` (Zipformer transducer,
GigaSpeech English, BPE-500, 16 kHz, chunk-16-left-64) is downloadable from
the `kws-models` GitHub release of k2-fsa/sherpa-onnx; tarball sha256
`f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a`
(17,626,723 bytes), Apache-2.0.
**Source:** https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2
**Evidence:** downloaded + unpacked 2026-07-11; contents: encoder/decoder/
joiner `-epoch-12-avg-2-chunk-16-left-64` in fp32 **and** int8 ONNX,
`tokens.txt`, `bpe.model` (SentencePiece), sample `keywords.txt` /
`keywords_raw.txt`, `test_wavs/`.
**Implication:** `scripts/fetch-models.sh` pins this sha256. The model's
algorithmic lookahead is fixed by its chunk-16-left-64 export — the bake-off
varies our pipeline `chunk_ms` on top of it, not the model's internal chunk.
The bundled `bpe.model` may let keyword compilation use SentencePiece
directly instead of a hand-rolled greedy tokenizer (verify in M1 phase 3).
**Confidence:** high (local artifact).
**Re-verify:** only if the release asset is re-uploaded (sha mismatch).

**Claim:** The model's `bpe.model` is (despite its name) a SentencePiece
**UNIGRAM** model (`trainer_spec.model_type = 1`), and greedy longest-match
tokenization against `tokens.txt` is WRONG for this vocab.
**Evidence:** protobuf field inspected via python sentencepiece 2026-07-11;
greedy diverges from spm on 4 of 12 known keyword vectors ("HEY SIRI",
"MERRY CHRISTMAS", "LIGHT UP", "FOR EVER" — e.g. greedy `▁LI G H T` vs spm
`▁ L IGHT`). No BPE merge path exists for e.g. `▁WORLD` (intermediates not
in vocab), proving non-BPE.
**Implication:** `kws_compile::TokenTable` implements unigram Viterbi
segmentation (max summed piece log-probs) with the 12 vectors + "recession"
/ "artificial intelligence" pinned as unit tests. Prefix (Fast) entries are
CUT from the full variant's token sequence, never independently encoded —
"ARTIFICIAL INTEL" encodes standalone to `… ▁IN T EL` but the transducer
emits `▁IN TE LL IG ENCE` mid-word, so an independently-encoded prefix can
never match.
**Confidence:** high. **Re-verify:** on any model swap.

**Claim (sherpa-onnx crate 1.13.4 KWS API, all pinned by local experiment
2026-07-11 on the gigaspeech-3.3M model):**
1. `keywords_buf` accepts in-memory keyword lines: `TOK TOK :boost
   #threshold @label`; `@label` round-trips verbatim as `result.keyword`.
2. `KeywordResult` exposes NO score/confidence field — gating is only via
   per-entry `#threshold`. `Detection.confidence` is therefore constant 1.0.
3. `start_time` is always 0.0. Per-token `timestamps` (40 ms granularity)
   look absolute BUT rebase unpredictably around leading/decoded silence
   (1 s decoded leading silence shifted times by −1.6 s; 5 s leading silence
   by −4.8 s; mid-stream silence between words does NOT rebase). They are
   NOT a reliable position source → `audio_pos_ms` uses our own fed-sample
   counter (hit position = end of the chunk in which it surfaced ≈ word end
   + decode lag ~0.4 s at chunk-16-left-64).
4. Detections re-fire without `reset()`; `reset()` is unnecessary and (per
   3) would corrupt the timeline. Refractory dedupe lives in our engine.
5. `create_stream_with_keywords` SILENTLY IGNORES its per-stream keyword
   list (config-level keywords fire instead).
6. Within one decoder, a prefix entry and its full entry are mutually
   exclusive per utterance (whichever path wins fires, the other never
   does) → two-tier isolation requires TWO spotters (Fast-only +
   Confirmed-only), which is what `SherpaKwsDetector` does.
7. Threshold interacts with entry length: `▁LOVE LY ▁CHI` fires at #0.05
   not #0.18; 1-token entries (`▁LOVE`) never fired at any threshold tried.
   Keep prefixes ≥2 tokens and tune thresholds per keyword (the bake-off
   dial). Measured prefix payoff on test_wavs: `▁LOVE LY` fires 640 ms
   before the full `▁LOVE LY ▁CHI L D`.
**Evidence:** examples/kws_probe.rs runs against test_wavs (known
transcripts), 2026-07-11.
**Confidence:** high. **Re-verify:** on sherpa-onnx crate upgrade.

## Open items carried forward

1. ~~Clock-skew tolerance~~ — RESOLVED: ±30 s, measured (see Q3).
2. **July 2026 fee-schedule update (7.7.26)** — fetch
   https://kalshi.com/docs/kalshi-fee-schedule.pdf from a real browser
   (Vercel blocks bots); diff vs Feb 2026 formulas above. Before coding the
   M2 EV gate.
3. **Demo credentials** — the key on this box is live-only; create a demo
   account/key before M2 demo order round-trips, then run `sign_probe demo`.
4. **Exact max batch size** — undocumented; bounded by write bucket only.
5. §12 Q7 (Rust SIP story) and Q8 (conference-provider landscape) — M3
   concerns, deliberately not researched yet.
