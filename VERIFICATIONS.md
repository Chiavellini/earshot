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

**Claim (MEASURED 2026-07-21 — corrects the "2.2 ms colo parity" reading): our
REST order host is a CloudFront edge; the matching engine is in us-east-2 (Ohio),
NOT us-east-1 where our box sits.** From the us-east-1 box, read-only DNS + TLS
handshake timings:
- `api.elections.kalshi.com` (the host `src/kalshi/rest.rs` posts orders to) →
  `3.167.99.x` = **AWS CloudFront**; TCP connect **~2.0–2.9 ms** (this is the CDN
  edge — the number the latprobe reported as "2.2 ms", NOT the engine).
- `external-api.kalshi.com` (real API backend) → CNAME
  `elections-external-api-…**us-east-2**.elb.amazonaws.com`; TCP connect **~17 ms**.
- `mm.fix.elections.kalshi.com` (FIX order entry) → `18.119/18.223.x` =
  **us-east-2**, no CDN; TCP connect **~12–14 ms**.
**Implication:** the true box→engine RTT is **~12–17 ms (a full region away)**, not
2.2 ms; the REST order path is CDN-fronted. Two levers follow: (1) move the box to
**us-east-2** (the engine's region); (2) FIX is the direct, CDN-free path to
us-east-2. The `decision→ack ~15–20 ms PREDICTED` in LATENCY-PROOF was, if anything,
optimistic about *where* those ms go. Real engine send→ack still needs a us-east-2
probe-order to pin.
**Source:** live probe from `i-04c50d76572aa092e` (44.197.147.18), 2026-07-21;
Kalshi FIX/PrivateLink docs (above); third-party us-east-2 reports corroborate.
**Confidence:** high (resolved hosts + handshake RTTs are direct measurements);
the exact engine send→ack awaits a us-east-2 probe-order.
**Re-verify:** after the us-east-2 box move — pin the real probe-order send→ack.

**Claim (CONFIRMED 2026-07-21 — the recovery, measured from us-east-2 itself):** a
throwaway t3.micro in us-east-2a measured TCP-connect RTT to the same hosts:
**external-api.kalshi.com (us-east-2 ELB) ~2 ms** (was ~17 ms from us-east-1) and
**FIX host ~0.8 ms** (was ~12–14 ms) → **box move recovers ~11–14 ms** on the
direct-to-engine path. `api.elections` stays a CloudFront edge (~2.5 ms) from both
regions — the CDN-free win is using the direct us-east-2 origin / FIX from a
us-east-2 box. Instance terminated after measuring (cost ~cents).
**Source:** `state/latprobe-useast2-2026-07-21.json` (probe `i-0ea6945c58b7af059`).
**Confidence:** high for the network RTT recovery; the per-order send→ack (warm
socket ≈ 1 RTT) still needs a relocated box + 1¢ probe-order to pin end-to-end.

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

**Claim (2026-07-14):** `GET /markets?event_ticker=<E>` returns each market with
the resolution text INLINE — `rules_primary`/`rules_secondary`, plus
`yes_sub_title` (the mention WORD), `yes_ask_dollars`, `can_close_early` — so the
event-prep stage needs no per-market fetch. Integer price fields (`yes_ask`) are
null; the `_dollars` string fields carry the price (consistent with the
mention-probe `yes_ask_dollars` pin). Verified event `KXEARNINGSMENTIONMS-26JUL15`
(Morgan Stanley, 2026-07-15): 14 markets, all `status=active`, none "said-N-times";
words incl. Tariff (ask $0.14), Recession ($0.36), Oil ($0.16), Middle East/Iran,
Buyback, Galaxy Digital, Tokenized, Zero Hash. Also `KXEARNINGSMENTIONC-26JUL14`
(Citi): 14 markets, Tariff $0.28 / Recession $0.21 / Inflation $1.00.
**Source:** live production API, consumed by `RestClient::event_markets` /
`earshot prepare`.
**Implication:** Event configs are auto-generated (`earshot prepare
--event-ticker`), not hand-typed. `earshot prepare --fillable` sets each cap to
`min(live ask, MENTION_FILL_CAP_CENTS=50)` — the smallest cap that crosses the
ask. A 1¢ mention order rests strictly below the ask (non-crossing, no fill); to
FILL requires the `mention_fill_gate` (`--fillable`), price ≥ ask, bounded by the
$1 probe ledger + notional caps.
**Confidence:** high (live, multi-event). **Re-verify:** per event at prep time
(strike suffix set + asks are per-event).

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
**July 7, 2026 update — verified 2026-07-12** from the live PDF ("Fee
Schedule for July 2026 - 7.7.26 Update", fetched in a real Chrome session;
text extracted from the PDF's content streams):
- Taker formula UNCHANGED: `fees = roundup(M × 0.07 × C × P × (1−P))`,
  default M = 1; "roundup rounds up such that the fee + position cost is
  rounded to a cent" (consistent with the live `average_fee_paid: 0.0007`
  evidence in §M2). `fee_cents` in `src/kalshi/executor.rs` remains correct.
- Maker fee formalized: `roundup(M × 0.0175 × C × P × (1−P))` with
  **default M = 0** — only series listed in the "Non-Standard Fees" table
  with a maker multiplier ≥ 1 pay maker fees.
- The Non-Standard Fees table (per-series maker/taker multipliers, e.g.
  KXBTCY 0/0, KXCPI 1/1) contains **no earnings-mention series** (searched
  all extracted pages for "mention", case-insensitive: zero hits) → mention
  markets keep taker M = 1, maker M = 0.
- Still no settlement fee, no membership fee.
**Confidence:** high (live PDF, 2026-07-12).
**Re-verify:** at the next dated schedule update (kalshi.com/fee-schedule
lists scheduled changes), or if `GET /exchange/series-fee-changes` flags an
armed series.

### Q5a — REST market-object price fields are dollars-only (pinned 2026-07-12)

**Claim:** On prod REST `GET /markets/{ticker}`, the integer-cent price
fields (`yes_ask`, `yes_bid`, `last_price`, …) come back **null**; prices
are only populated in the fixed-point dollar-string fields
(`yes_ask_dollars`, `yes_bid_dollars`, `last_price_dollars`, …) — same
protocol drift as the WS book feed (see Q1/book pins).
**Evidence:** `probe-order --allow-mention` on
KXEARNINGSMENTIONJPM-26JUL14-TARI refused with "no best ask visible"
while the market showed ask $0.16 (2026-07-12 drill); fixed by parsing
`yes_ask_dollars` via `book::dollars_to_cents`.
**Implication:** never read integer price fields from REST market objects.
**Confidence:** high (live). **Re-verify:** on API version announcements.

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

**Claim (2026-07-15): Kalshi quantities are FRACTIONAL contracts (hundredths),
and sub-cent price structures exist.** The 2026-07-10 assumption that `*_fp`
quantities are whole contracts ("23844.00") has drifted: the live MS run
(state/KXEARNINGSMENTIONMS-26JUL15/autorun.log, 06:05–07:30 CDMX) logged
thousands of fractional book quantities ("0.77", "0.30", "9888.38"), and REST
market objects now carry `*_fp` sizes plus `price_level_structure` +
`price_ranges`: KXEARNINGSMENTIONMS-26JUL15-RECE shows `volume_fp: "3075.67"`
and `price_level_structure: "linear_cent"` (step 0.0100); KXMVE* markets show
`"deci_cent"` (step 0.0010 = 0.1¢ price levels). Consequences (both landed
2026-07-15): book quantities parse EXACTLY as centi-contracts
(`book.rs::fp_to_centi_contracts` — the old whole-contract truncation deleted
sub-1.0 levels and let deltas drift; it cost us the true best-ask during the
MS run), and the arm-time validator REFUSES any market whose
`price_level_structure` is present and not `linear_cent` (the book prices in
integer cents).
**Confidence:** high (pinned live, both wire layers).

**Claim:** Measured order-path latency from this laptop: send→ack ~90–94 ms
(RTT-dominated), exec→send 352 µs with pre-signed headers + pre-staged body
(spec §3 stage-7 "<1 ms" target confirmed achievable). The ≤60 ms
detection→ack gate is judged from the M5 region, not from here.
**Confidence:** high (measured, n=2 sessions).

**⚠ Operational:** this Kalshi account carries **~52 resting orders from
other systems**. Any cancel logic must be scoped to the session's own order
ids (`OrderRegistry`) — an account-wide cancel-all would destroy unrelated
positions. This is now hard-coded policy in `rest.rs::cancel_orders`.

**Claim (code landed 2026-07-13; empirical numbers PENDING — see Open item 6):**
The orders `RestClient` now sets `tcp_nodelay(true)` (Nagle off) and h2
keep-alive (`http2_keep_alive_interval` 30 s + `_while_idle`), and
`main.rs::arm` keeps the orders connection warm — a one-shot pre-warm after
`wait_for_books` plus a 60 s `GET /exchange/status` keep-warm ping, both under
the 90 s `pool_idle_timeout` — so the first live POST never pays a cold
TLS+TCP handshake. ALPN is left to negotiate (NOT `http2_prior_knowledge`,
which would break the h1 wiremock tests). Separately, `send→ack` is now split
at TTFB into `send→hdrs` (network + matching engine) and `hdrs→ack` (body read
+ JSON parse), and the ledger disk write was removed from the fire path
(`record_send` measured 27 ns/op, `perf_risk_record_send`), so `exec→send` no
longer contains a blocking write.
**Evidence (code):** `src/kalshi/rest.rs` builder + `warm`/`spawn_conn_warmer`;
`src/main.rs::arm` wiring; `tests/perf_budget.rs::perf_risk_record_send`.
**Pending (live):** negotiated wire version to the orders origin (h1 vs h2),
connection reuse across orders, and the `send→hdrs`/`hdrs→ack` split — captured
by `probe-order --allow-mention` (now prints `send→hdrs / hdrs→ack / send→ack
[VERSION]`).
**Confidence:** medium (code landed + unit/mock-verified; live wire numbers
pending). **Re-verify:** reqwest upgrade / Cloudflare edge change / any
first-order latency regression.

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

## streaming_ctc engine pins (2026-07-12)

**Claim:** `ort` (onnxruntime bindings) and `sherpa-onnx` **cannot both static-
link onnxruntime** in one binary — use `ort` `load-dynamic` + a runtime dylib.
**Source:** live build/test on this box (aarch64-apple-darwin), 2026-07-12.
**Evidence:** `sherpa-onnx-sys` 1.13.4 statically links onnxruntime **1.24.2**
(API 24; `SHERPA_ONNX_STATIC_LIBS` includes `onnxruntime`). `ort` 2.0.0-rc.12
requests API **27**. With both static, the linker resolves the shared `Ort*`
symbols to one copy per binary: the `tests/sherpa_kws` binary picked 1.24.2 →
`"requested API version [27] is not available … Current ORT Version is: 1.24.2"`
→ **SIGSEGV**; the lib-test binary picked ort's 1.27 and worked (fragile/
nondeterministic). Fix: `ort = { features = ["ndarray", "load-dynamic"] }` (keep
defaults — `default-features=false` fails ort's own compile on a missing VitisAI
EP field). ort then dlopens onnxruntime from `ORT_DYLIB_PATH`; sherpa keeps its
static 1.24.2; no clash. Verified: full suite green (88 lib + all integration
incl. sherpa_kws) with `ORT_DYLIB_PATH` → `libonnxruntime.1.27.1.dylib`.
**Implication:** vendor `onnxruntime-osx-arm64-1.27.1` (tgz sha256
`e42b77a7281cc6e55141bf44fcfbac2c782b823a491bbb6ac33c781dd991f8a6`);
`scripts/fetch-models.sh` downloads it and prints
the `export ORT_DYLIB_PATH`. Only the streaming_ctc engine needs it (lazy load on
first Session); sherpa_kws-only runs are unaffected. Linux/CI needs the matching
`onnxruntime-linux-*-1.27.x` dylib.
**Confidence:** high. **Re-verify:** on `ort` or `sherpa-onnx` crate upgrade
(check the onnxruntime versions still differ / API bump).

**Claim:** The streaming zipformer2-CTC ONNX exposes CTC **frame posteriors** and
a fully self-describing streaming-state signature; fbank matches via pure-Rust
`kaldi-native-fbank`.
**Source:** `sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01`
(Apache-2.0) driven via `ort`, 2026-07-12 (Chinese stand-in for plumbing only).
**Evidence:** inputs `x [N,77,80]` + 74 state tensors; output `log_probs
[N,T',1000]` = per-frame log-posteriors. Metadata: `model_type=zipformer2`,
`T=77`, `decode_chunk_len=64`. Feeding: 77-frame window, advance 64; caches
carried (`new_X`→`X`). Init states derived generically from the signature (batch
dim → 1, zero-fill). Greedy decode: silence→0 tokens, 0.wav→31, 1.wav→26
(content-dependent ⇒ posteriors correct). `kaldi-native-fbank` 0.1.0 is pure Rust
(the C++ `knf-rs` binding fails to build — CommandLineTools libc++ headers
unresolvable) with exact HTK mel `1127·ln(1+f/700)`; config: 80-bin, use_energy
false, dither 0, snip_edges false. Geometry here: `decode_chunk_len=64`→640 ms
chunk step, `T−chunk=13`→~130 ms right-context (NOT ≤80 ms — this is a plumbing
proxy; the English model needs small `decode_chunk_len`).
**Implication:** the English model is a drop-in provided it exports the same
signature (an icefall causal `--decode-chunk-len ~8` zipformer-CTC does).
Re-derive `kws_compile` pinned vectors if its BPE vocab ≠ gigaspeech-500.
**Sourcing (scoped 2026-07-14, `docs/CTC-MODEL-SOURCING.md`):** the export script
`icefall/egs/librispeech/ASR/zipformer/export-onnx-streaming-ctc.py` already emits
this exact signature — the gap is purely a **checkpoint** trained `--causal 1
--use-ctc 1`. No published English streaming×CTC checkpoint found (shelf English
streaming models are transducers; shelf English CTC checkpoints are non-causal).
Effort tiers: T0 hours/CPU if such a checkpoint is found, T1 GPU-hours to finetune
a CTC head onto a causal transducer, T2 GPU-days from scratch. `bench-detector
--engines streaming_ctc` panic-safe as of 2026-07-14 (bench configs now carry a
`[detector.streaming_ctc]` section).
**Confidence:** high (plumbing); the ≤350 ms latency number awaits the English
model + paced bake-off.

**Claim (2026-07-17):** an English causal+CTC streaming zipformer EXISTS and
drives the streaming_ctc engine — the Tier-0 outcome of the sourcing plan.
**Source:** `sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18` (GitHub
k2-fsa/sherpa-onnx `asr-models` release; tar.bz2 sha256
`cd8ed7371b1a0c49e919549a606235c8092f9006425aa06a9e8e6395054126bd`, pinned in
`scripts/fetch-models.sh`). Checkpoint provenance:
`csukuangfj/icefall-asr-librispeech-streaming-zipformer-small-2024-03-18` (HF).
**Evidence:** (a) training command in the HF repo's
`run-streaming-zipformer-ctc.sh` shows `--causal 1` AND `--use-ctc 1`
(full LibriSpeech, small config); training log confirms `'causal': True,
'use_ctc': True`, 23.76 M params; icefall PR #1557 names this repo.
(b) ONNX metadata read from the int8 export: `model_type=zipformer2`, `T=45`,
`decode_chunk_len=32` → **320 ms decode quantum, ~130 ms right-context**
(vs the zh stand-in's 640 ms/130 ms and sherpa_kws's ~320 ms lookahead +
0.4–0.5 s decode lag). (c) live on this box 2026-07-17: both streaming_ctc
integration tests PASS against it (`EARSHOT_REQUIRE_MODELS=1`; measured
`out_per_chunk = 8`), and `compile-keywords` encodes all bench keywords on
its LibriSpeech BPE-500 with zero tokens.txt gaps (cuts differ from
gigaspeech — recomputed, e.g. guidance → `▁G U ID ANCE`).
**Caveats:** the CTC head was an auxiliary loss (`ctc_loss_scale 0.2`) with
NO published WER — the corpus bake-off is the sole quality gate. The 130 ms
right-context exceeds the 0–80 ms follow-on ambition; the checkpoint (.pt
epochs 26–30 on HF) was trained with chunk 16/32/64, so a chunk-16 re-export
(160 ms quantum) via `export-onnx-streaming-ctc.py` is a legitimate
latency-trim follow-on if the paced bench lands near the 350 ms bar.
**Confidence:** high (identity/format/plumbing); detection quality TBD by
`bench-detector` dev/holdout. **Re-verify:** n/a (immutable release asset).

## Q-M3 — Twilio Media Streams request signature

**Claim:** Twilio signs each request with
`X-Twilio-Signature = base64(HMAC-SHA1(authToken, URL + concat(sorted POST params)))`,
where `URL` is the exact request URL (for `<Stream>`, the `wss://…` stream URL)
and params are each `key`+`value` concatenated in lexicographic key order. A WS
upgrade is a GET with no body params, so the signed string is the URL alone;
Media Streams `<Parameter>`s are delivered in the `start` message, not the URL,
and do not participate in the upgrade-request signature.
**Source:** Twilio "Validating requests" security docs (HMAC-SHA1 scheme,
official as of 2026-07-12); RustCrypto `hmac`+`sha1` for the primitive.
**Evidence:** the HMAC-SHA1 primitive is pinned by an RFC 2202 known-answer
vector (`hmac_sha1_matches_rfc2202_vector`); the URL-concat scheme + constant-
time compare are round-trip tested (`signature_round_trips_and_rejects_tampering`).
**Implication:** `TwilioWsSource` validates in the `accept_hdr_async` handshake
(`valid_twilio_signature`); mismatch → 403. Token from `TWILIO_AUTH_TOKEN`; the
exact configured stream URL may be pinned via `TWILIO_STREAM_URL` (else rebuilt
from `Host` + request target).
**Confidence:** **live-verified 2026-07-14** (loopback rehearsal, ngrok-fronted):
three real Twilio streams accepted with `TWILIO_STREAM_URL` pinned as the
signature base — "X-Twilio-Signature verified" on every handshake, no 403s.
Evidence CallSids `CA919fb7…` (outbound_track TTS rehearsal, 1280 frames, 3/3
keyword detections), `CA5831c4…` (inbound_track vs the real MS bridge via
`twilio-dial.sh` + DTMF, 3585 frames, 0 drops), `CA2dfec9…` (full
`launch-event.sh` chain, 2674 frames, 0 drops, clean STOP+hangup teardown,
report.json written). Message shapes (`connected`/`start` with
mediaFormat audio/x-mulaw 8 kHz mono/`media`/`stop`) matched the pinned docs
shapes on all three calls (`docs/TWILIO.md` stamped live-verified).
**Also live-found 2026-07-14:** a TCP connection that fails the WS handshake
used to kill the armed pipeline (single-accept). Fixed: the listener now drops
failed handshakes and keeps waiting until `accept_timeout`
(`tests/twilio_source.rs::stray_connection_does_not_kill_the_listener`).

## Q-MSPM — MS 2026-07-15 RECE race: the loss was upstream of the order path

> ⚠ **AMENDED 2026-07-27 — RECE SETTLED NO. The latency finding stands; the framing
> "the single genuine race" does not, and the race had negative EV.**
>
> `state/KXEARNINGSMENTIONMS-26JUL15/strike-close.json` →
> `KXEARNINGSMENTIONMS-26JUL15-RECE: {"status":"finalized","result":"no"}`. Of 14 MS
> markets only **INVE and TAIL** settled YES.
>
> **Unaffected — everything this pin actually measures.** The tape timestamps, the
> ~400 ms audio-arrival deficit, `capture→decision` 91.3 ms, the warm-socket
> decomposition and the "lever is upstream" implication are all timing facts and are
> unchanged. This pin's technical content survives intact.
>
> **Affected — how it is quoted.** RECE was **not a winnable race**: at our 26¢ cap a
> fill returns **−26¢**. So this pin must no longer be cited as evidence that *winning
> races requires closing audio latency* — it shows only that we were **late**, not that
> being early would have paid. The competitor's 27¢→95¢ sweep was itself a **wrong-way
> bet**, the same pattern later found at AAL (WORL/CENT/IRAN) and INTC
> (ARIZ/DESK/GOVE/MOBI). Standing rule: **sweeps ≠ mentions.**
>
> See `docs/COMPETITOR-ANATOMY.md` §"The three true-positive races" correction box.

**Claim:** the single genuine race of the MS run (RECE, cap 26¢, IOC canceled
unfilled) was lost on **audio-arrival latency**, not order-path latency: the
first competitor fills printed on the public tape ~400 ms BEFORE the audio
containing "recession" reached our pipeline input.
**Source/Evidence (2026-07-16):** public trades endpoint, no auth —
`GET https://api.elections.kalshi.com/trade-api/v2/markets/trades?ticker=KXEARNINGSMENTIONMS-26JUL15-RECE`.
Tape: batch fills at **13:27:10.273707Z** sweep YES 27¢→95¢ (50 @ 93¢,
49.23 @ 95¢, 124.77 @ 81¢, …), continuing to 96–98¢ at 10.393702/10.506997.
Our run artifacts (`state/KXEARNINGSMENTIONMS-26JUL15/report.json` +
`autorun.log:2130`): decision logged 13:27:10.763424Z with
`capture→decision` 91.3 ms ⇒ audio arrival ≈ 13:27:10.672; order acked
+104 ms later into a book already at 96–98¢. Decision-time snapshot said
best_ask 20¢ — stale book vs MM re-quote undecidable post-hoc (executor now
records `book_age` per order).
**Also pinned:** the trades endpoint returns `taker_book_side`/`taker_side`,
dollar-string prices, fractional `count_fp`, and is publicly readable for
settled markets — usable for post-event race forensics on any future run.
**Implication:** connection/presign reuse cannot win this race (both were
already live during the run, commit 8421741; `presign_age` 1.17 s is
time-since-refresh on a 10 s cycle, zero latency cost; `send→hdrs` 102 ms
matches a warm-socket CDMX RTT — vs the 128–134 ms COLD numbers in item 6
below, confirming the warmer worked). The lever is upstream: audio transport
(bridge→Twilio→ngrok) and detector chunking (~60 ms of the 91 ms internal
edge). Full decomposition: `docs/RUNBOOK.md` follow-up 2.
**Confidence:** high — public tape cross-referenced against our own
monotonic-to-wall mapping (±ms via the decision log line).

## Q-FIX — FIX order entry (lever E2; offline build 2026-07-21)

All facts researched 2026-07-21 against live docs.kalshi.com FIX pages
(fix/connectivity.md, fix/authentication.md, fix/order-entry.md,
fix/common-components.md, fix/error-handling.md, fix/subpenny-pricing.md).
**Nothing here is live-verified yet** — FIX access is Premier-gated. The
offline build (`src/kalshi/fix/`) codes ONLY to the F-facts below; the open
questions are parked as verify-at-live-logon items and the code stays
tolerant where they bite.

**F1 — Protocol.** FIXT.1.1 transport, application version FIX50SP2
(`8=FIXT.1.1`; Logon `1137=9`).
**Source:** fix/connectivity.md, fix/common-components.md. **Confidence:**
high (documented). **Re-verify:** demo logon.

**F2 — Endpoints.** Prod order entry `mm.fix.elections.kalshi.com`, port
8228 = `KalshiNR` (no retransmission), 8230 = `KalshiRT` (retransmission,
institutional approval). Demo `fix.demo.kalshi.co`. TLS 1.2+ mandatory (AWS
NLB); one FIX connection per API key. (Hosts also pinned in Q1.)
**Source:** fix/connectivity.md. **Confidence:** high.

**F3 — Logon auth.** 35=A carries `96=RawData` = base64 RSA-PSS/SHA-256
signature over the pre-hash `SendingTime SOH MsgType SOH MsgSeqNum SOH
SenderCompID SOH TargetCompID` (SOH = 0x01); SendingTime must byte-match tag
52. Same RSA private key as REST; `49=SenderCompID` is the FIX API key UUID.
This is a DIFFERENT canonical string than REST's `{ts_ms}{METHOD}{path}` —
implemented as `Signer::sign_raw` + `fix::encode::logon_prehash`.
**Source:** fix/authentication.md. **Confidence:** high on the field list;
trailing-SOH ambiguity is open question OQ7. **Re-verify:** demo logon.

**F4 — Logon tags.** `98=0`, `108=N` with N > 3 s (default 30; config
enforces > 3), `1137=9`, `95`/`96` (95 immediately precedes 96). `141=Y`
REQUIRED on KalshiNR. `8013` CancelOrdersOnDisconnect defaults N — we KEEP
N (see OQ4). `21005=Y` (subpenny dollar prices) deliberately NOT set.
**Source:** fix/authentication.md, fix/subpenny-pricing.md. **Confidence:**
high.

**F5 — Session mechanics.** `52` format `YYYYMMDD-HH:MM:SS.mmm`, must be
within ±30 s of server time (else Reject 373=10). MsgSeqNum from 1,
monotonic. Standard BodyLength/CheckSum (mod-256, 3 digits).
**Source:** fix/common-components.md. **Confidence:** high.

**F6 — NewOrderSingle (35=D).** Required: `11` ClOrdID (UUID preferred,
≤64 chars — our 36-char deterministic UUID fits; SAME idempotency key as
REST), `38` OrderQty, `40=2` (Limit only), `44` Price = **integer cents
1–99** in default mode, `54` Side, `55` Symbol = market ticker. Optional:
`59=3` IOC, `18=6` post-only, `2964` STP (`1`=Taker-At-Cross default,
explicit in our template), `21006`, `21009`.
**Source:** fix/order-entry.md. **Confidence:** high on names/codes; `38`
wire shape is OQ2.

**F7 — ExecutionReport (35=8).** `150` ExecType (0 New / F Trade / 4
Canceled / 8 Rejected / C Expired / A PendingNew…), `39` OrdStatus, `37`
exchange OrderID, `14` CumQty, `151` LeavesQty, `31` LastPx (cents), `32`
LastQty, `6` AvgPx, `103` OrdRejReason. IOC: Trade ER(s) then terminal;
nothing crossed → Canceled/Expired.
**Source:** fix/order-entry.md. **Confidence:** high.

**F8 — Cancel.** 35=F requires `11` (new id) + `41` OrigClOrdID + `54` +
`55`; 35=9 OrderCancelReject carries `102` + `58`. Encoded for completeness;
NOT wired into the kill switch (which cancels OrderRegistry ids over REST —
scope invariant).
**Source:** fix/order-entry.md. **Confidence:** high.

**F9 — Session errors.** Reject 35=3 (45 RefSeqNum, 373 reason 0–11),
BusinessMessageReject 35=j (380: 6=Not authorized, 8=Rate limit). Seq too
low on Logon → terminated. Gaps recoverable ONLY on KalshiRT (24 h
lookback); on NR a gap is fatal → reconnect fresh (our machine's posture).
Kalshi never resets seqnums during maintenance.
**Source:** fix/error-handling.md, fix/authentication.md. **Confidence:**
high.

**F10 — Rate limits.** FIX drains the SAME token buckets as REST (no
throughput headroom — the wire buys latency only); Logon itself is
rate-limited (reconnect backoff floor 1 s); Heartbeat/TestRequest/Logout are
free.
**Source:** fix/connectivity.md. **Confidence:** high.

**Open questions — resolve at demo logon, do NOT guess in code:**
- **OQ1** Server-side CompIDs on inbound messages (`Kalshi` vs the session
  id `KalshiNR`?) — parser/machine are tolerant (log-only mismatch).
- **OQ2** `38` OrderQty wire shape: plain `38=60` vs `38=60.00` ("decimal"
  documented without example; REST required `"60.00"` — Q1). Golden vectors
  use plain integer; FLAGGED.
- **OQ3** Side semantics for the NO leg (54 on YES leg like REST bid/ask?).
  Non-blocking — we only ever buy YES (`54=1`).
- **OQ4** `8013=Y` scope: "all open orders" on disconnect — session-scoped or
  account-wide? Account carries unrelated resting orders → stays `N` until
  verified.
- **OQ5** Whether the FIX API key id equals the REST `KALSHI_API_KEY_ID` or
  is separately provisioned — config `[kalshi.fix] fix_key_id` overrides,
  falls back to the REST key id.
- **OQ6** RSA-PSS salt length for the Logon signature (assumed digest-length,
  same as the REST signer primitive).
- **OQ7** Trailing SOH after TargetCompID in the Logon pre-hash (assumed
  none — separators only).
- **OQ8** Heartbeat grace behavior server-side (we probe at 1.2×, drop at
  2× — standard FIX practice, undocumented by Kalshi).

**Implication:** the fire path can swap `POST /portfolio/events/orders`
(CloudFront-fronted, us-east-1→us-east-2 ≈ 12–17 ms box RTT, see Q1 2026-07-21
revision) for a persistent session to the FIX gateway (~0.8 ms from us-east-2,
`state/latprobe-useast2-2026-07-21.json`). Offline build landed 2026-07-21
behind `[kalshi] transport = "fix"` (default `rest`, behavior unchanged).
**Live validation ladder:** demo logon (resolves OQ1–OQ8) → prod logon → 1¢
probe under the $1 ledger → FIX-vs-REST send→ack A/B from the same box.

## Open items carried forward

1. ~~Clock-skew tolerance~~ — RESOLVED: ±30 s, measured (see Q3).
2. ~~July 2026 fee-schedule update (7.7.26)~~ — RESOLVED 2026-07-12: taker
   0.07 unchanged, maker 0.0175 default-M=0, no mention series in the
   non-standard table (see Q5).
3. **Demo credentials** — the key on this box is live-only; create a demo
   account/key before M2 demo order round-trips, then run `sign_probe demo`.
4. **Exact max batch size** — undocumented; bounded by write bucket only.
5. §12 Q7 (Rust SIP story) and Q8 (conference-provider landscape) — M3
   concerns, deliberately not researched yet.
6. ~~Order-connection latency evidence~~ — RESOLVED 2026-07-14: `probe-order
   --allow-mention` against live Mentions markets (KXEARNINGSMENTIONMS-26JUL15-
   TARI and -RECE, both active): negotiated **HTTP/1.1**, cold single-shot CLI
   runs measured `build+sign` 2.6–3.3 ms, `send→hdrs` 128–134 ms,
   `hdrs→ack` 26–31 µs (send→ack ≈ 128–134 ms). These are COLD numbers (each
   CLI invocation opens a fresh TLS+TCP connection); the armed pipeline's
   pre-warmed connection should shave the handshake share. **Warm sample
   captured 2026-07-15 (MS live run): `send→hdrs` 102 ms on the armed
   pipeline's kept-warm connection — ~30 ms under cold, consistent with the
   handshake share saved (see Q-MSPM).**

## Q-FILL — First detection-triggered mention FILL (2026-07-23, INTC live)

- **Pinned (2026-07-23, live evidence):** the full hear-keyword → cross-ask →
  FILL loop executed for the first time. KXEARNINGSMENTIONINTC-26JUL23-APPL:
  Confirmed detection (streamlink leg, us-east box arm) → decision intent
  21:20:38.999689Z → crossing IOC at cap 28¢ → **public tape prints our
  1-contract YES taker trade at 26¢, 21:20:39.009549Z — decision→matched-trade
  9.9 ms** including the us-east→Kalshi order leg. Order record in
  `state/KXEARNINGSMENTIONINTC-26JUL23/report-box.json` (`order_status:
  "filled"`); ledger debited 28¢ (worst-case notional at send; fill was 26¢).
- Envelope held exactly: 7 orders total = 4 observe non-crossing (1¢, never
  sent), 2 LOST_RACE refusals (TRUM ask 19¢ > cap 11¢; CLI ask 34¢ > cap 27¢ —
  the frozen-cap "no pump" guard working), 1 sent+filled. Cumulative ledger
  28¢ of 100¢.
- Caveat pinned with the win: APPL last-traded 1¢ post-call — the fill was a
  detector FALSE FIRE ("apple" class). **Settlement CLOSED 2026-07-23: APPL
  finalized NO at 23:29:46Z ⇒ realized P&L −26¢** (1×YES @26¢ → 0). **TRUM
  also finalized NO (same batch) ⇒ the frozen-cap LOST_RACE refusal at 19¢
  was a correct save.** The fill MACHINERY is validated; the trade's edge is
  a detector-precision question (Phase-4). Evidence: box `~/intc-live.log`,
  tape `state/KXEARNINGSMENTIONINTC-26JUL23/tape-2026-07-23.json`,
  settlements `state/KXEARNINGSMENTIONINTC-26JUL23/settlements-2026-07-23.json`
  (API `result:"no"`, status `finalized`). All five 07-23 settlements pinned
  same evening: LMT PERU/IRAN **NO** (false fires), **NASA YES** (true flag
  the market ignored 52 min — market-missed-word channel settlement-confirmed)
  in `state/KXEARNINGSMENTIONLMT-26JUL23/settlements-2026-07-23.json`.

## Q-OX — OpenExchange / KnowledgeVision venue class (2026-07-28, Ford Q2, live)

- **Venue anatomy (live-verified):** the OpenExchange event page
  (`*.open-exchange.net/webcast`, login-gated registration) embeds a
  **KnowledgeVision** player iframe. The top frame requests only
  `passport-api.open-exchange.net/event/<slug>` (event state) and a
  `view.knowledgevision.com/presentation/redirect/<id>?...` URL carrying the
  registrant identity in query params. **The m3u8 never appears in the top
  frame's network log** — open the KV redirect URL directly and the player
  requests it in-frame.
- **Stream shape:** master = `.../live/stream/PRODUCTION_<id>_0/index.m3u8`
  (`#EXT-X-STREAM-INF`, 3 renditions `index_1/2/3.m3u8`), HLS v3,
  `#EXT-X-TARGETDURATION:6`, `#EXT-X-PLAYLIST-TYPE:EVENT` (grows from seq 1 —
  a catch-up fetcher will replay the pre-live standby), `#EXT-X-PROGRAM-DATE-TIME`
  present (arrival log works). **No EXT-X-PREFETCH, no LL-HLS** — floor ≈ 1–1.5
  segment durations; measured edge lag ~9 s behind PDT.
  ⚠ **EVENT playlists break any bounded URI-set dedup** (live-measured, Ford
  07-28, RUNBOOK bug 4): the manifest held 497 segments at fetcher start and
  grew past 1,150, so a fixed 500-key `seen` cap evicted still-listed URIs
  every poll and the leg sawtoothed to ~the whole manifest behind the edge
  (arrival p50 98 s, 1 segment in 146–657 delivered). hls-edge-fetch.py now
  scales the cache with the manifest; the first-variant rule also cost here —
  the master lists 5.86 Mbps 1080p FIRST and 638 kbps 640x360 later, and the
  fetcher now picks the cheapest rendition.
- **Auth split (evidence: live curl from the us-east box, 2026-07-28
  ~20:52 UTC):** `index.m3u8` + variants are **UNSIGNED and public** (200 from
  the box with no cookies ⇒ Path A) while `broadcasting.txt`/`player_config.txt`
  on `content.knowledgevision.com` are CloudFront-signed. `playlist.m3u8` /
  `master.m3u8` are 403 (S3 AccessDenied) — `index.m3u8` is the master's name.
- **Speculative probing: NO-GO** — unannounced-segment probes 403
  (`SPECULATIVE: NO-GO` from manifest-probe; fetcher self-disabled correctly).
- Full run evidence: `state/KXEARNINGSMENTIONF-26JUL28/postrun/` (arrival log
  n≈large, spec_edge SUMMARY, probe output) + RUNBOOK Ford 07-28 entry.

## Q-CC — Chorus Call / Dynamic Media Frame venue class (2026-07-29, WING + SBUX)

Two Wednesday events ride this venue (WING 14:00 UTC, SBUX 20:15 UTC), and it
is the first venue where the *"has it started"* signal is separable from the
*"give me the audio"* credential. Live-probed from the us-east box 2026-07-29
~02:40 UTC; every line below is a curl result, not doc reading.

- **Page identity.** `event.choruscall.com/mediaframe/webcast.html?webcastid=<id>`
  serves a JS `webcastConfig` object with the durable per-event facts:
  `webcastId`, `frameId` (== the form's hidden `streamId`), `accountName`,
  `eventTitle`, `eventDate` (with an explicit `eventDateTimezone`), and
  `eventStatus`. **WING = `Cv4NzoL0` / frameId `28574`**; SBUX Q1 FY26 archive
  = `Im3S8SaF` / frameId `20448`.
  ⚠ `eventDate` is rendered with a **named US zone** (`"Wed Jul 29 10:00:00
  EDT 2026"` + `eventDateTimezone: "America/New_York"`), i.e. this venue does
  NOT reproduce the EST-for-EDT misprint seen on the PYPL/META/CVNA IR widgets.
- **UNAUTHENTICATED live-detector (the useful finding).**
  `GET event.choruscall.com/mediaframe/v2/dynamicframe/getPolledStatus?streamid=<frameId>`
  returns a bare integer, **no cookies, no registration, no browser**, from the
  us-east box in **93 ms**:

  | streamid | returned | meaning |
  |---|---|---|
  | 28574 (WING, upcoming) | `0` | not started |
  | 20448 (SBUX Q1, archived) | `3` | archived |

  `1` == live (from `commonScripts.js` `handleEventStatus`: the player reloads
  on 0→1 and treats 1 as the live frame). This gives a browser-free arm trigger
  keyed to the venue's own state rather than to a wall-clock guess — the first
  venue where we have that.
- **The m3u8 is NOT reachable this way.** `webcast.html` returns `pageId:
  "register"` for BOTH an upcoming and an archived event, and the page contains
  **zero** `.m3u8` / `streamUrl` / `mediaUrl` strings. The player page
  (`pageId: "frame"`) is served only to a registered session; the form POSTs to
  `preregistration/submit` with `firstName/lastName/email/company` (+ hidden
  `streamId`, `loginType`). **`loginType` differs per event — WING = 2, SBUX
  Q1 = 1** — so the registration shape is per-event and must not be assumed.
- **`wss://webcast.choruscall.com/webcast-cmt/socket` is a CONNECTIVITY TEST,
  not stream delivery** (`commonScripts.js` `websocket:` test block closes it
  immediately unless `pageId == 'frame'`). Do not build a resolver against it —
  an earlier read of this file suggested the m3u8 arrives over that socket and
  that is wrong.
- **Not yet pinned:** the shape of the stream URL on the `frame` page, and
  whether the emailed confirmation link carries a token that bypasses the form.
  Both are unresolved as of this entry — do not guess them in code.

## Q-OPS — Operator-screened bridges defeat the listen-only dial (2026-07-29, WING, live)

**A published dial-in with no passcode is NOT the same as an automatable one.**
`configs/dial-ins.toml [WING]` recorded `tier: published`, `listen_access: yes`
on the strength of the IR release quoting two numbers and no participant code.
Live-dialled 2026-07-29 ~03:35 UTC (CallSid `CA420e89f4999c89af78362d25b748ac14`,
`+14123175176`, listen-only, **no DTMF sent**, 33 s, $0.014) and the tier is
right about the numbers and wrong about the access:

```
[ 0.00- 4.00] Welcome to Chorus Call. Please hold for the Wingstop Conference.
[ 4.00- 7.00] A Conference Specialist will be with you shortly.
[ 7.00-25.00] (hold music)
[27.00-29.00] Welcome to Chorus. …how can I help?     <- LIVE HUMAN OPERATOR
[29.00-33.00] (our silence) -> operator disconnects
```

Evidence: `corpus/bridge-probes/wing-2026-07-29-bridge-entry.wav` (8 kHz μ-law
via Twilio Media Streams), transcript by faster-whisper base.en on a +6 dB
16 kHz resample; per-second RMS shows the announcement, the hold music, and the
operator turn as distinct energy bands.

- **No passcode exists** — so DTMF was never the obstacle, and `--dtmf` would
  not have helped. The obstacle is a human asking who you are.
- **We cannot answer, by design.** `docs/TWILIO.md` listen-only invariant: audio
  is one-way by construction and DTMF is passcode-entry only. There is no
  supported path for the system to speak to a screener, and adding one would
  breach the invariant that keeps us out of the Q&A queue. **This is a hard
  architectural block, not a missing feature.**
- **Therefore: an operator-screened bridge is UNATTENDED-INFEASIBLE.** It needs a
  human on the line at join time. Do not schedule one into a human-absent window.
- ⚠ **Generalize before trusting any `listen_access: yes`.** The registry field
  was inferred from published numbers, never from a dial. MSFT is the documented
  counter-example — its operator preamble ("all participants are in listen-only
  mode… press star one") proves an AUTO-JOIN bridge, so the field is genuinely
  per-issuer. **Probe-dial before relying on any bridge unattended**; the probe
  costs ~1.4¢ and 33 s. Bridges that answer with an announcement + hold music +
  a specialist are Chorus-Call-operator-assisted and fail this test.
