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
edge). Full decomposition: `docs/history/RUNBOOK.md` follow-up 2.
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
- **RESOLVED same night — the player reads ONE named field.** `frameScripts.js`
  builds its video.js source list from `webcastConfig.hlsSource` and branches on
  the extension, so the stream URL is EMBEDDED IN THE PAGE, not pushed over the
  STOMP/SockJS channel the frame page also loads:

  ```js
  if (isValidURL(webcastConfig.hlsSource, 'http-hls'))  push({type:'application/x-mpegurl'})  // .m3u8
  else if (isValidURL(webcastConfig.hlsSource,'http-mp4')) push({type:'video/mp4'})           // .mp4
  else if (isValidURL(webcastConfig.hlsSource,'http-m4a')) push({type:'audio/mp4'})           // .m4a
  ```

  Confirmed against the live archive fixture: `webcastConfig.hlsSource =
  "http://vodchoruscall.akamaized.net/07452/starbucks/starbucks260128.mp4"`.
  **Read the field by name; do not regex the page for URLs** (the scrape stays
  only as a backstop). ⚠ **A Chorus Call stream may legitimately be PROGRESSIVE
  rather than HLS** — `hls-edge-fetch.py` speaks manifests and cannot read it,
  which is why `configs/wing-progressive-arm.toml` (one plain ffmpeg leg)
  exists and `choruscall-resolve.py --allow-progressive` emits a `.kind` file.
  `mediaType` is the audio/video bit: **0 = audio-only** (the player is a 30 px
  control bar; WING and the SBUX archive are both 0), 1 = video.
- **`eventStatus` 4 = expired** — observed live when the SBUX archive lapsed
  mid-session and the page flipped to `pageId: "expired"`. A resolver must treat
  any non-1 status as "keep polling / fail closed", never assume 0/1/3.
- **Still not pinned:** whether the emailed confirmation link carries a token
  that bypasses the form (untested — programmatic registration made it moot).
- Evidence: transcript inline above; the probe wav is laptop-local and
  UNTRACKED (`corpus/` is gitignored) at
  `corpus/bridge-probes/wing-2026-07-29-bridge-entry.wav`.

### Q-CC LIVE URL SHAPE — now OBSERVED, twice (2026-07-29, WING + SBUX, live)

The open item above ("the URL shape of a genuinely LIVE Chorus Call event, which
no probe has yet observed") is **closed**. Both of today's Chorus Call events were
captured live and the shape is identical:

```
https://pmd.choruscall.com/httporiginlive/<STREAM-ID>_http/playlist.m3u8
                                          └─ master; lists one chunklist.m3u8
```

- WING 14:0x UTC: `…/httporiginlive/Cv4NzoL0_http/playlist.m3u8` → HTTP 200,
  `#EXT-X-STREAM-INF:BANDWIDTH=71410,CODECS="mp4a.40.2"`, one `chunklist.m3u8`.
  Captured 85 min on it (arm log `wing-arm-20260729T140620Z.log`).
- SBUX 20:10 UTC: `…/httporiginlive/6216819_http/playlist.m3u8` → HTTP 200,
  same shape, `BANDWIDTH=71259`. Captured from 5 min before the call.

⚠ **`<STREAM-ID>` IS NOT ALWAYS THE `webcastid`.** This is the trap and it nearly
cost SBUX. WING's happened to be its webcastid (`Cv4NzoL0`), so the pattern looked
like `{webcastid}_http`. SBUX's is a **numeric id** (`6216819`) bearing no relation
to its webcastid (`Vwxw4BrF`) — a URL built from the webcastid 404s forever, and
the live gate simply never flips (silent miss, exactly the WING failure mode).
The numeric id is **not** `frameId`/`streamId` from `webcastConfig` either (SBUX
`frameId` was a different number). It is only observable from the LIVE player's
own network traffic, after registration.

**Implication for `choruscall-resolve.py`:** the template is a *probe candidate*,
never a substitute for reading the player. Correct order: (1) resolve `hlsSource`
from the player as today; (2) only if that stays empty, probe
`httporiginlive/<webcastid>_http/playlist.m3u8` and `validate()` it before use;
(3) never assume the webcastid form is the live id. A browser-equivalent fetch of
the live player page remains the only general way to get `<STREAM-ID>`.

**Also observed:** the live manifest carries **no per-segment
`#EXT-X-PROGRAM-DATE-TIME`**, which is why `hls-edge-fetch.py --arrival-log`
wrote a 0-byte file for all of WING (fixed same day: records now fall back to
fetch wall-clock, tagged `-nopdt`).

- Evidence: browser network capture (WING + SBUX live players), `curl` from the
  us-east box returning 200 on both, and both arms' fetcher SUMMARY lines
  (`wing_spec_edge.err`, `sbux_spec_edge.err`) showing segments flowing.
  Confidence: high (two independent issuers, same day, both captured).
  Re-verify: any Chorus Call event where the numeric-vs-webcastid form differs
  again, and whenever `pmd.choruscall.com` changes hostname.

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

> ⚠ **AMENDED 2026-07-29 (same day, evening).** The MSFT "auto-join
> counter-example" bullet above is WRONG — it was inferred from an earnings-call
> transcript preamble, not from a dial. A live dial the same evening reached an
> INCOM **operator screen** ("please be ready to provide your full name with
> spelling; an operator will assist you momentarily") that dropped the silent
> caller in 42–73 s. See Q-ADMIT below. **Unaffected:** everything else in this
> pin — the WING evidence, the invariant analysis, and the probe-before-trust
> rule (which this amendment only strengthens: even the pin's own counter-example
> didn't survive a dial). **Affected:** any claim that MSFT joins unattended.

## Q-NGROK — one free static endpoint ⇒ only ONE phone lane at a time (2026-07-29, live)

Measured on the us-east box while CMG's phone leg was live: starting a second
`ngrok http` agent — different local port, different `web_addr`, with and
without a per-event config — fails immediately:

```
ERR_NGROK_334  failed to start tunnel: The endpoint
'https://sondra-unshivering-derelictly.ngrok-free.dev' is already online.
Either 1. stop your existing endpoint first, or
       2. start both endpoints with `--pooling-enabled` to load balance between them.
```

The free plan issues ONE static domain and every agent session is assigned it;
random per-session hostnames are no longer handed out, so a second concurrent
tunnel has nowhere to land. `--pooling-enabled` is NOT a fix here — it load
balances across the endpoints, which would split one call's media between two
listeners.

**Implication:** `docs/MULTI-EVENT.md` §8 concurrency (one process per event,
per-event `--bind-port` + tunnel) holds for the WEBCAST lane, but the PHONE lane
is capped at **one simultaneous event** on this ngrok account. 2026-07-29 hit
this directly: CMG (20:30) and MSFT+CVNA (21:30) overlap, so at most one of them
can run a phone leg. Concurrent phone events need either a paid ngrok plan
(multiple reserved domains) or the no-ngrok path already approved in
`docs/INGRESS-BAKEOFF.md` (TLS terminating on the box itself, which also removes
a whole hop of latency from the lane we most care about).

- Evidence: `/tmp/ngrok2.log`, `/tmp/ngrok3.log` on the box, 2026-07-29 20:27 UTC.
  Confidence: high (reproduced twice, distinct configs). Re-verify: on any plan
  change, or after the no-ngrok migration lands.

> ⚠ **AMENDED 2026-07-29 (same day, evening).** The no-ngrok path is now BUILT
> (Caddy TLS ingress: `deploy/caddy/Caddyfile`,
> `scripts/install-tls-ingress.sh`, `launch-event.sh --tls-host`) and is the
> production answer to this cap. **Unaffected:** every fact above — the free
> plan still serves one endpoint, and ngrok remains the loopback-rehearsal
> path, still capped. **Affected:** production concurrency planning — once
> Q-TLS is pinned (live call through the new endpoint), the phone lane's
> one-event cap is lifted; do not provision paid ngrok or second accounts.

## Q-TLS — TLS-on-box ingress carries a live Twilio media stream (2026-07-30, live)

**PINNED.** One live call to the TTS rehearsal number (+17627212978) through
`wss://media.3-212-174-94.sslip.io/e/8099` — Caddy terminating TLS on the box,
reverse-proxying to the loopback listener. CallSid
`CA6b1e301e669cf4703ddfe0221a3f345d`, 2026-07-30 03:14 UTC.

- **X-Twilio-Signature verified through the proxy** — Caddy preserved Host and
  path exactly (the Q-M3 hazard the Caddyfile header warns about). `connected`
  → `start` (mediaFormat verified 8 kHz μ-law mono) → 2801 media frames in
  55.8 s → clean stop at the probe's 60 s cap. Zero stalls ≥100 ms.
- **Hostname:** `media.3-212-174-94.sslip.io` — sslip.io wildcard DNS mapping
  to the Elastic IP `3.212.174.94`; Let's Encrypt issued normally
  (`certificate obtained successfully`, caddy journal 03:13:07 UTC). This is a
  zero-cost stopgap name: it moves with nothing (encodes the IP), but the
  namespace is shared and LE rate limits on sslip.io are communal — an owned
  domain should replace it when available; only `EARSHOT_DOMAIN` changes.
- **Ingress jitter, same TTS number, same probe tool, same box** (caveat: one
  day apart, not interleaved — but the effect size dwarfs any daily drift):

  | path | p50 | p90 | p99 | max | stalls ≥100 ms | <1 ms bursts |
  |---|---|---|---|---|---|---|
  | ngrok (07-29, `tts_rehearsal_2026_07_29.jsonl`) | 16.67 ms | 35.11 ms | 226 ms | 460 ms | 61 | 31.5 % |
  | TLS-on-box (07-30, `tts_tls_2026_07_30.jsonl`) | 20.00 ms | 20.04 ms | **20.10 ms** | **20.46 ms** | **0** | 0.4 % |

  The TLS path delivers native Twilio 20 ms pacing; the tunnel's burst/stall
  signature (INGRESS-BAKEOFF's stated p99 hazard) is absent entirely.
- **Consequences:** the Q-NGROK one-event cap is lifted for production (per-event
  `/e/<port>` paths 8080–8085 + 8099 are routed); ngrok remains the
  loopback-rehearsal fallback. Launch with `launch-event.sh --tls-host
  media.3-212-174-94.sslip.io`.
- Evidence: `corpus/bridge-probes/tts_tls_2026_07_30.{json,jsonl}`; caddy
  journal on the box. Confidence: high. Re-verify: after any instance/EIP move
  (the sslip.io name encodes the IP — it must be re-derived), on domain swap,
  or caddy config change.

## Q-REDIAL — mid-call drop recovered in-run, production mechanism (2026-07-30, live)

**PINNED.** Full rehearsal via `scripts/launch-event.sh` (pinned binary, TLS
ingress `--tls-host media.3-212-174-94.sslip.io`, `configs/rehearsal-qredial.toml`
with `max_reconnects = 2`, `idle_timeout_secs = 8`) against the TTS rehearsal
number, with a REAL induced far-end hangup (REST `Status=completed` — the exact
MSFT drop signature) and a REAL SIGTERM teardown. Timeline (box, 04:01 UTC):

- 04:01:36.25 call1 `CA68449c37944a6ac21cab806b0742a383` stream started (attempt 0)
- 04:01:47.32 detection fired pre-drop ("gaming" → Confirmed intent → designed 1¢ observe)
- 04:01:48.10 far-end hangup POSTed (HTTP 200)
- 04:01:48.18 source: `stream stopped frames=595`; marker written
  `{"unix_ts":1785384108,"old_call_sid":"CA684…","attempt":1,"reason":"stop"}`
- 04:01:48    supervisor consumed the marker BY CONTENT in the same wall-clock
  second (the old mtime watch would have missed it), logged far-end status,
  hung up the zombie concurrently, redialed with NO backoff:
  **drop → new CallSid 0.60 s**
- 04:01:52.21 call2 `CA34389d…` stream connected+started (attempt 1):
  **drop → fresh audio 4.03 s** (previous floor: 10–15 s + up to 30 s detection)
- 04:02:03.03 post-splice detection: "gaming" heard again on call2 and
  **correctly IGNORED by the one-shot keyword state machine** — detection
  works across the splice AND the no-double-fire property holds across redials
- 04:02:28    real SIGTERM: clean last-words report (`"terminated":"SIGTERM"`),
  ledger 0¢, no session orders

Evidence: `/tmp/qredial-launch2.log` + `state/KXEARNINGSMENTIONAAPL-26JUL30-QREDIAL/redial.log`
on the box. Confidence: high. Re-verify: after any change to
`twilio_ws.rs` reconnect handling, `redial-supervisor.sh`, or the launcher wait.
NOTE: round 1 of this rehearsal produced a false NO-REDIAL because the harness
never sourced Twilio creds — the hangup silently no-opped and no drop occurred.
When a persistence rehearsal shows no redial, first verify the drop actually
happened (call status/duration), then blame the code.

## Q-OPERATOR-V1 — human-first operator handler, full ladder (2026-07-30, live)

**PINNED.** Complete rehearsal of the gated operator path via the PRODUCTION
mechanism: `scripts/launch-event.sh --operator-handler` (pinned binary, TLS
ingress, `configs/rehearsal-qoperator.toml` with `[operator_handler]
enabled=true, cell_number="+525545436717"`), against the fake-operator number
**+17622164408** (`scripts/fake-operator/server.py --base-path /fake-operator`
on the box behind Caddy, VoiceUrl `https://media.3-212-174-94.sslip.io/fake-operator/`).

Ladder observed (04:18–04:20 UTC):
1. Arm → `listener.ready` → **page call to the human cell ANSWERED and joined
   the conference** (page sid `CA170c20a3d50c7e34350f16cc46309a37`); the bridge
   was dialed ONLY after the join confirmed (`--wait-joined`).
2. Bridge leg `CA93d84edb1abd3cc6f5a8ff5107f93f83` → fake operator answered,
   asked name/company (`<Gather input="speech">` heard speech at /step2 and
   /step3 — Twilio ASR garbled the words but the human's answers advanced the
   screen, as with a real operator) → **admitted**.
3. Keyword script played post-admission and **all three armed keywords
   detected** on the bridge leg's inbound track while conference-parked:
   gaming 04:19:48.02, competition 04:19:52.50, acquisition 04:19:59.22 —
   Confirmed intents, dry-run journaled.
4. Real SIGTERM: clean last-words (`"terminated":"SIGTERM"`), ledger 0¢.

Fail-closed also proven live (attempt 1, same night): when the pager refused
(gate flag missing from the orchestration — since fixed), the launcher REFUSED
to dial the bridge unmanned and tore down cleanly in 2 ms. The unmanned-silent-
leg drop class (MSFT/CVNA/WING 21–73 s) is structurally unreachable.

Registry consequence: YUM (admission=operator_screened, probed same night)
gains `operator_handler = "rehearsed-2026-07-30"`; preflight downgrades its
FAIL to PASS-with-note only when the config enables the handler.
Evidence: `/tmp/qoperator-launch.log` + `/tmp/fake-operator.log` on the box.
Confidence: high. Re-verify: after any change to the operator scripts, the
launcher orchestration, or conference TwiML shapes (re-run this ladder).

## Q-CONF — `<Start><Stream>` fork vs `<Dial><Conference>` (2026-07-30, live; both steps pinned)

**Claim (step 1, PINNED):** a `<Start><Stream track="inbound_track">` fork started
before `<Dial><Conference>` KEEPS FLOWING once the call enters the conference,
carrying the far end's audio. TTS rehearsal call `CAc3d1273db27d66f46f738b720dccd605`
parked in conference `qconf-1785381966`: 2783 frames / 55.4 s of a 60 s window,
0 stalls, through the Q-TLS ingress. The captured WAV transcribes to the full
TTS keyword script (gaming / Hollywood / competition / ad-supported /
acquisition — `corpus/transcripts/qconf-2026-07-30.txt`), so the fork carries
real program audio while conference-parked, at native 20 ms pacing.
**Step 2 (PINNED, same night):** a second conference participant's voice (the
paged human cell) stays OFF the inbound track. `--with-cell` run
`qconf-cell-2026-07-30`: bridge leg `CAd4f03d34672b632a9a6c079364bea2c8` +
cell leg `CA9ed50cb1f7b31767b0e91be67537bc21` to +525545436717 — ANSWERED,
63 s on the line, the human spoke the marker phrase ("earshot marker check one
two three") into the live conference. Captured 3541 frames / 70.6 s; the
transcript (`corpus/transcripts/qconf-cell-2026-07-30.txt`) is the TTS script
VERBATIM — the marker phrase is entirely absent. Conference-injected audio
does not reach `inbound_track`.
Side-pin: outbound voice to +52 (Mexico) works on this Twilio account — no
21215 geo-permission block.
**Implication:** operator handler v1 carries the capture fork on the bridge
leg itself — no stream handoff needed after admission; the human answers the
operator without contaminating detection audio. Confidence: high (both steps).
Re-verify: on any Twilio Media Streams change.
Evidence: `corpus/bridge-probes/qconf_2026_07_30.{json,jsonl}`,
`corpus/bridge-probes/qconf_cell_2026_07_30.{json,jsonl}`,
`corpus/transcripts/qconf-2026-07-30.txt`,
`corpus/transcripts/qconf-cell-2026-07-30.txt`.

## Q-UPDATE — live TwiML replacement vs a running `<Start><Stream>` (2026-07-30, live)

**Claim (PINNED):** `POST /Calls/<sid>.json` with replacement TwiML does NOT
kill a running `<Start><Stream>` fork — the fork is CALL-scoped, not
TwiML-document-scoped. TTS call `CA0440befa2f2c7a21ea8af0d2be5a568b`: TwiML
replaced with `<Response><Pause length="120"/></Response>` at ≈20 s
(status `in-progress` at POST time); frames_before=1011, frames_after=1070,
stream ran 41.4 s total (far end's script ended it), 0 stalls.
**Implication:** mid-call REST re-steering is compatible with an uninterrupted
capture fork. This enables (a) v2 clip-play/hold flows, and (b) a v1 ESCAPE
HATCH: a listen-only call that unexpectedly hits an operator screen can be
REST-redirected into a conference (+ page the human) without losing capture.
Confidence: high. Re-verify: on any Twilio Media Streams behavior change.
Evidence: `corpus/bridge-probes/qupdate_2026_07_30.{json,jsonl}`.

## Q-ADMIT — per-venue bridge admission, observed by dial (2026-07-29, live)

**What a bridge does to a silent listen-only caller is a per-venue fact that only
a dial can establish.** Four venues dialled live on 2026-07-29; two classes
emerged. Every line below is from a recording of the bridge's own audio
(diagnostic Twilio calls; transcripts by faster-whisper small.en int8 on the
laptop, `.venv-corpus`). DTMF timing is RULED OUT as a drop cause: diagnostic
calls that sent **no digits at all** died at the same hold times as calls that
sent them (CVNA: digits at 10 s repeated 3× → dropped 31 s; no digits → 21 s).

### Class A — automated passcode bridge: HOLDS

- **CMG** `+1 888 317 6003` code `3937444#`: admitted by code, **held 4572 s
  (76 min)** with 4 live detections until we killed the leg ourselves (21:13 UTC,
  to free the ngrok tunnel — Q-NGROK). CallSid `CAeb4f8bbaa252ebb41dbbb2b7532c4eaf`.
  Continuous conference audio the whole way. This is the venue class the
  unattended phone lane can serve today.

### Class B — live-operator screen: DROPS the silent caller in <90 s

- **MSFT** `+1 877 407 0666` (no code): INCOM conference center.
  ```
  [ 0.00- 2.40] Welcome to the INCOM conference center.
  [ 2.40- 5.36] Please be ready to provide your full name with spelling.
  [ 5.36- 7.96] An operator will assist you momentarily.
  [ 7.96-36.40] Conference center, may I have your first and last name, please?  <- LIVE HUMAN
  [37.40-43.40] Hello? Anyone on the line, are you on mute? Hello?
  ```
  4 calls, held 42/73/42/48 s, all `status=completed` (far end hangs up).
  Drop time is independent of dial timing (16 min early, 3.5 min early, and
  after call start all died identically).
  Artifact: `corpus/bridge-probes/msft-2026-07-29-bridge-entry.wav` + `.txt`
  (45 s, 8 kHz; Twilio recording `REa618e98bb5337dc33a933760c864799c`).
- **CVNA** `+1 800 715 9871` code `3609424#`: Chorus-style operator screen. The
  code is irrelevant — a human answers before any code prompt.
  ```
  [ 0.00- 2.88] Well, thank you for holding.
  [ 2.88- 4.80] May I have the name of your conference?                          <- LIVE HUMAN
  [ 8.44-11.56] Hello caller, I can hear you. Please check if your line's on mute.
  [15.04-18.84] Apologies … due to no response, I will now disconnect this caller. Thank you.
  ```
  Held 23/31/21 s. The operator announces the disconnect policy out loud.
  Artifact: `corpus/bridge-probes/cvna-2026-07-29-bridge-entry.wav` + `.txt`
  (19 s, 8 kHz; Twilio recording `RE7791b0336482835f219224343faf3026`).
- **WING** `+1 412 317 5176`: Chorus Call operator screen, 33 s — see Q-OPS
  above (the founding member of this class).

**Implications:**
- `configs/dial-ins.toml` gains a probe-only `admission` field (contract in the
  file header): paper sources (IR page / PR / 8-K) can establish the NUMBER but
  never ADMISSION — MSFT was recorded `listen_access="yes"` ("no password
  required") and drops us in 42 s.
- Class B venues are unattended-infeasible until the gated operator-responder
  path exists (approved 2026-07-29); until then they must FAIL preflight for a
  phone arm.
- Class A/B is decidable the night before with one ~$0.02 call. Preflight
  requires a dated probe.
- Confidence: high (MSFT ×4 calls, CVNA ×3, CMG 76 min, WING ×1). Re-verify: a
  venue's admission pin is stale after 30 days or any change of conferencing
  provider (the bridge greeting names the provider: INCOM, Chorus Call).

### Q-ADMIT addendum — 2026-07-30 off-hours probes (YUM, AAPL)

- **YUM +18007159871 (code 8719077#): Class B — operator_screened.** Probe
  2026-07-30 03:44 UTC, CallSid `CA79973ec7ad974d27e56d10162304f1d6`: answered
  immediately into an INCOM-style queue — "Welcome to the conference center.
  Please wait for the next available operator." Held 118.7 s / 5945 frames in
  queue (overnight, no operator picked up; WE hung up at the probe cap). The
  MSFT RULE vindicated again: published digits + "all lines placed on mute"
  transcript language looked automatable; the live dial found a screen.
  Evidence: `corpus/bridge-probes/yum_2026_07_30_bridge_entry.{wav,json,jsonl}`,
  `corpus/transcripts/yum-2026-07-30-bridge-entry.txt`.
- **AAPL +18002160480 (code 650941): reservation INACTIVE off-hours.** Probe
  2026-07-30 03:40 UTC, CallSid `CAdf6b601752f28876b7db0c3536773e83`: rang 55 s,
  Twilio `status=no-answer`, zero media. A standing conference reservation
  (numbers+code byte-identical in Wayback 2024-02-01→2026-07-02) that answers
  only around the event. NEW SUB-FACT for the probe playbook: off-hours probes
  cannot classify venues whose bridges activate per-event — those need a
  pre-call-window probe (T-90..T-30 min) on event day.

## Q-WNT — ABC World News Tonight: a DAILY mention market (2026-07-29, live) (lane removed 2026-08-12)

**Claim:** `KXWORLDNEWSMENTION` ("World News Mention", category Mentions) is a
**daily** market on ABC's flagship TV newscast. Event ticker
`KXWORLDNEWSMENTION-<yyMONdd>`; today `KXWORLDNEWSMENTION-26JUL29`, sub_title
`"any host or reporter - ABC World News Tonight with David Muir"`. Airs
6:30 PM ET = **22:30Z = 16:30 CDMX**, ~22 min. Board = **16 strikes**, all
`can_close_early: true` / `early_close_condition: "This market will close and
expire early if the event occurs."`. `close_time`/`expiration_time` are a
2026-08-13T14:00:00Z **placeholder** while the board is open. ⛔ This pin then
read that placeholder as "early close on utterance is the real mechanism, same
as `KXFEDMENTION-26JUL-VOLA` (Q4)" — **that inference is WRONG and is
superseded by Q-WNT-CLOSE**: across all 16 settled sessions the board closes in
one ~23:05Z batch and no strike has ever closed on an utterance. Prices are
live and two-sided (`-TRUM` 30/31¢, `-HIKE` 25/26¢, `-AI` 18/19¢).
**Source:** `GET /trade-api/v2/series/KXWORLDNEWSMENTION`,
`GET /trade-api/v2/events?series_ticker=KXWORLDNEWSMENTION&status=open&with_nested_markets=true`,
`GET /trade-api/v2/markets?event_ticker=KXWORLDNEWSMENTION-26JUL29` (live prod).
**Implication:** ⚠⚠ **14 of the 16 strikes are FIRST-UTTERANCE strikes** — pure
latency races, the `good afternoon` archetype called out in
`configs/webcast-fed-26jul29-arm.toml` ("decided PURELY by the race — not by
detection quality"). Only 2 are cumulative-count strikes (`-TRUM` "Trump (5+
times)", `-IRAN` "Iran (3+ times)"), the sole ones where prediction beats
speed. And it is DAILY: every fix amortises across ~250 sessions/yr instead of
one call. Highest-leverage market found so far.
**Confidence:** high (live API, 16/16 markets read). **Re-verify:** the strike
board is regenerated per session — re-read `rules_primary` and the
count-vs-first-utterance split at every arm; `earshot prepare --event-ticker`
handles it identically to any Mentions event.

**Claim:** The series was **structurally invisible to our own discovery job**
until 2026-07-29. `scripts/discover-events.py` swept
`PREFIXES = ("KXEARNINGSMENTION", "KXFEDMENTION")`, and even after adding the
prefix, `call_date()` reads the airdate out of `sub_title` — which for a
recurring broadcast carries **no date at all** — so every session landed in
`UNRESOLVED` and dropped out of the horizon list and `slate/worklist.md`.
**Source:** the series is only findable by hand-listing
`GET /series?category=Mentions` (**397 series** as of 2026-07-29, up from the
379 pinned in Q4) and reading titles.
**Implication:** Third instance of the Boeing/FOMC class. Fixed by
`DAILY_SERIES` in `discover-events.py` — a narrow per-series opt-in that takes
the airdate from the event-ticker suffix and tags it
`call_date_source: "ticker_daily"`. Deliberately NOT a general
date-from-ticker inference (commit `6be80aa`: "HOOD was not a
date-from-ticker bug — Kalshi's metadata lies in stereo"), and
`ticker_date_misleading` reports `null` for these rows because comparing a
ticker against a date derived from that ticker can only ever agree with
itself — reporting `false` would claim "checked and clean", the exact HOOD
failure mode. Regression frozen in `--self-test`.
**Confidence:** high. **Re-verify:** re-list `series?category=Mentions` monthly
and diff the titles — a prefix sweep cannot discover a family it doesn't know.

**Claim (venue):** YouTube (`youtube.com/@ABCNews/live`) is the reachable
ABC News Live feed and it carries WNT, but it is a **hostile venue for our
best fetcher**: `hls-edge-fetch.py` reports
`speculative: DISABLED — segment URLs are not predictable` because YouTube
signs every segment URL (`/sig/…`, `/lsig/…`), and `prefetch=NONE-SEEN`. The
+1.02 s pre-announcement lead measured on the DW reference stream
(`docs/LATENCY-PROOF.md` §THE GAP IS LOCATED) is **not available here** — the
leg degrades to manifest-driven polling. The manifest is a **DVR playlist**
(`playlist_type/DVR`, ~2886-segment window, `EXT-X-TARGETDURATION:5`), so
`media_sequence` is the window START, not the edge; ffmpeg `-live_start_index -1`
did land on the true edge. `streamlink` has **no plugin for
`abcnews.go.com/Live`** and that page yields no `m3u8` on a plain fetch, so the
direct ABC packager is still unreconned.
**Source:** live capture 2026-07-29 22:12–23:03Z, laptop (residential IP;
the us-east box is YouTube bot-walled — `docs/RACE-STRATEGY.md:159`).
`configs/webcast-abcwnt-capture.toml`, `~/abcwnt/spec_edge.err`.
**Confidence:** high (observed). **Re-verify:** YouTube URL signing is a
platform property — re-check only if a non-YouTube ABC manifest is found.

**Claim (clock trap):** YouTube stamps `EXT-X-PROGRAM-DATE-TIME` in **Eastern
time but labels it `+00:00`** — a **4 h** label skew. Measured: wall
22:12:17Z against `last_pdt 2026-07-29T18:12:10.016+00:00`. After correcting
+4 h, the manifest edge sits within roughly ±3 s of wall clock.
**Implication:** ⚠ `scripts/abs-lag.py` computes
`arrival_utc − (segment_PDT + EXTINF)`. Fed this manifest uncorrected it
returns a ~4-hour "lag" — garbage that looks like a catastrophic finding. Any
absolute-lag run against a YouTube venue MUST apply the offset (or calibrate
it per run; do not hardcode 4 h — it is the venue's local-time offset, so it
moves with US DST). Note also that PDT only anchors the CDN/packager output;
the studio→packager contribution is invisible to us and shared by every
internet consumer, so **the decisive number remains Δrace against the Kalshi
tape**, not absolute lag.
**Confidence:** high (measured, n=370 polls). **Re-verify:** every venue,
first run — a `+00:00` label is not evidence of UTC.

## Q-WNT-SRC — ⛔ ABC News Live does NOT carry World News Tonight (2026-07-30) (lane removed 2026-08-12)

**Claim:** The 2026-07-29 WNT capture recorded **the wrong program**, and every
conclusion drawn from it about detector accuracy or race latency is void.
`KXWORLDNEWSMENTION` resolves on words said "as part of **ABC World News
Tonight with David Muir**" — the linear ABC *television network* newscast.
**ABC News Live** (`youtube.com/@ABCNews/live`, the free 24/7 streaming
channel) is a DIFFERENT service with its own schedule, and on 2026-07-29 it did
not simulcast WNT at 22:30Z.

**Source:** faster-whisper transcript of our own 50-minute capture
(`corpus/transcripts/abcwnt-26jul29.{txt,words.json}`, 8 337 words; preserved
as `abcwnt-26jul29.ABCNEWSLIVE-WRONGSOURCE.{txt,words.json}` — see the
filename warning at the end of this pin):

- `"world news tonight"` and `"david muir"` occur **only at 22:24:34–22:24:45Z**
  — inside an ADVERTISEMENT, six minutes before air: *"World News Tonight with
  David Muir is the number one newscast in America… Thank you for making World
  News Tonight with David Muir the number one newscast in America."*
- At 22:30:00Z the stream continues its OWN programming mid-sentence
  (*"…Lindsay's medical timeline asking how many medications she was
  prescribed. Turning to the Middle East…"*). Signatures of that show —
  `"top searches"` (22:42:03, 22:43:54), `"the spiral"` (22:43:04, 22:44:00),
  `"welcome back to search"` — are ABC News Live's own trending-news format,
  hosted by someone addressed as "Oshin". No `"good evening"` cold open.
- Words that **settled YES** are absent from the audio entirely:
  `blanche` 0, `maintain` 0, `unchanged` 0, `midterm` 0, `midterms` 0
  occurrences in 8 337 transcribed words. An ASR does not miss "maintain" and
  "midterm" wholesale; they were never said on the feed we recorded.

**Implication — what this retracts:**

1. ⛔ **The "+12.9 s race win" on `FIFT` is RETRACTED.** The detection at
   22:30:23.460Z was "**Fifth** Amendment" in ABC News Live's Fauci segment,
   not the `FIFT` occurrence on WNT. Both programs covered the same news day
   (Iran strikes, Fauci invoking the Fifth, the Fed decision), which is
   precisely why the coincidence was convincing — and why cross-leg
   corroboration did not catch it. **Two legs agreeing tells you the two legs
   heard the same audio; it says nothing about whether that audio is the
   right program.** No valid Δrace exists for this market yet.
2. The apparent recall collapse (4 of 6 said-words undetected) was mostly a
   WRONG-SOURCE failure, not a detector failure. The threshold sweep
   (0.15→0.05, recall flat at 2/6) was measuring the wrong audio and proves
   nothing about the operating point.
3. The variant-study/autopsy verdicts DO stand — they are TTS-based and
   independent of the capture: `iraq` is genuinely NO-EVIDENCE (piece `Q`
   logP −10.46), `tariff`/`ice` NEAR-MISS, `midterm` WEAK, `blanche` fires
   strongly on clean speech.

**What the market actually requires:** the **linear ABC network feed**. Free
ABC News Live is not a substitute. Candidate obtainable sources, none yet
verified: a live-TV subscription carrying the local ABC affiliate (Hulu + Live
TV, YouTube TV, DirecTV Stream, Fubo), or off-air ATSC on physical hardware at
a US address. This reopens the question the operator raised first — that
capturing the *actual broadcast* is the hard part — and closes the
"co-listener on the same feed" reading inherited from the earnings lane.

**Note on the promo, which may be its own signal:** the 22:24:20Z teaser
previews the headline words — *"tracking the flooding … Dr. Anthony Fauci,
grilled by lawmakers, invokes the fifth, and the Fed's decision on interest
rates"* — and `FIFT` and `MAIN` (maintain/unchanged, i.e. the Fed decision)
both settled YES. A teaser cannot RESOLVE the market (it is not part of the
newscast) but it predicts it, ~6 minutes early. Unverified as a repeatable
edge; recorded so it is not lost.

**⚠ Filename hazard, found 2026-07-30 while banking the right programme.**
macOS is case-insensitive, so a transcript written as
`corpus/transcripts/ABCWNT-26JUL29.words.json` **is the same file** as this
pin's `abcwnt-26jul29.words.json` and silently overwrites it. Banking the
correct broadcast under an ABCWNT- id would have replaced the wrong-source
proof with right-source audio under the name this pin cites — a quiet
substitution of exactly the kind that let the original error stand. Hence
`scripts/wnt-vod-fetch.sh` uses the `WNTVOD-` prefix, and the evidence above is
duplicated to an explicit `…ABCNEWSLIVE-WRONGSOURCE…` name. Both files are
gitignored (`corpus/transcripts/`), so git would not have caught the loss.
**Confidence:** high (independent ASR over our own recording, plus explicit
program signatures). **Re-verify:** per session — ABC News Live's schedule is
not a constant, and a day when it *does* carry WNT would be visible as a
`"good evening"` cold open plus `world news tonight` inside 22:30–23:00Z.

## Q-WNT-CLOSE — the board does NOT close on first utterance (2026-07-30, live)

**Claim:** `can_close_early: true` / `early_close_condition: "This market will
close and expire early if the event occurs."` is advertised on every
`KXWORLDNEWSMENTION` strike, but **it has never been observed to fire**. Across
all **16 settled sessions** (`26JUL06`…`26JUL29`, 221 markets) the board closes
in a **single batch**, modally **23:05:4xZ** — ~35 min after air, ~13 min after
the newscast ends. Decisive discriminator: **YES and NO strikes close in the
same second.** If utterance triggered the close, YES strikes would close
scattered across 22:30–22:52Z and NO strikes would all close together at the
end. Measured `last YES close` vs `first NO close` per session:

| session | last YES | first NO | | session | last YES | first NO |
|---|---|---|---|---|---|---|
| 26JUL14 | 23:05:34 | 23:05:32 | | 26JUL23 | 23:10:50 | 23:10:49 |
| 26JUL22 | 23:05:41 | 23:05:40 | | 26JUL24 | 23:05:44 | 23:05:42 |
| 26JUL29 | 23:05:44 | 23:05:42 | | 26JUL28 | 23:05:43 | 23:05:42 |

(Four sessions — `26JUL08`, `26JUL16`, `26JUL21`, `26JUL25` — carry stragglers
closing hours later, up to 15:26Z next day. Those are administrative late
closes, and they are spread across YES and NO alike, so they are not utterance
closes either.)
**Source:** `GET /trade-api/v2/markets?event_ticker=KXWORLDNEWSMENTION-<d>`
for all 16 settled events, live prod, 2026-07-30.
**Implication:** ⚠ **supersedes the Q-WNT reading** ("early close on utterance
is the real mechanism, same as `KXFEDMENTION-26JUL-VOLA`"). The market stays
open through the whole broadcast. The race is therefore against **other
traders**, not against an exchange clock — and that race is contested and fast.
The 26JUL29 public tape shows one taker sweeping each book from ~9¢ to ~99¢:
`IRAQ` 22:31:15.325→22:31:16.048 (0.72 s), `BLAN` 22:38:16.018→22:38:16.976
(0.96 s), `MAIN` 22:40:29.432→22:40:30.069 (0.64 s), `MIDT`
22:43:42.471→22:43:43.462 (0.99 s). Nothing is left after the sweep.
⚠ **A narrow burst is NOT evidence that the pack is near the broadcast** — it
shows the leaders agree with each other, which is the same trap as cross-leg
corroboration in Q-WNT-SRC. The pack's absolute offset is Q-WNT-PACK.
**Confidence:** high (16/16 sessions read live). **Re-verify:** monthly, and
immediately if Kalshi changes `early_close_condition` — an actual early close
would invert the trading model back to what Q-WNT assumed.

## Q-WNT-VOD — the real broadcast IS obtainable, just late (2026-07-30, live) (lane removed 2026-08-12)

**Claim:** ABC publishes the actual newscast, free, two ways:

1. **YouTube VOD, same night.** `@ABCNews` posts
   *"ABC World News Tonight with David Muir Full Broadcast - <Month D, YYYY>"*,
   ~1170–1220 s. Verified for `26JUL29` = video `4ALcFr5aQYU`, 1206 s.
   Reachable by dated channel search
   (`/@ABCNews/search?query=World+News+Tonight+Full+Broadcast+<Month>+<D>`),
   which ranks the target first — a `/videos` crawl would need hundreds of
   entries, since ABC posts ~25 videos a day.
2. **ABC News Live replays.** The same free 24/7 stream that does *not* carry
   WNT at 22:30Z **does** replay it later: 2026-07-30 listings show WNT at
   **8:00, 9:30, 10:30 and 11:30 PM ET**. So the wrong-programme feed of
   Q-WNT-SRC is the right programme 90 minutes later.

**Source:** `yt-dlp --flat-playlist` against the dated channel search
(2026-07-30); TV Passport listing for ABC News Live, station 36224.
**Implication:** unblocks everything that is not the live race. Real broadcast
audio + the settled board = the honest detector-accuracy measurement that
2026-07-29 was supposed to produce and could not. Banked by
`scripts/wnt-vod-fetch.sh` into `corpus/wnt/<yyMONdd>/`.
⚠ **The VOD has advertisements removed** (~1200 s of content in a 22-minute
slot), so a word's offset is **programme time, not wall clock minus 22:30Z**.
Anything mapping these offsets to UTC must model the breaks;
`scripts/wnt-pack-lag.py` does, and refuses to report when it cannot.
⚠ Neither route is tradeable: both land long after the ~1 s sweep (Q-WNT-CLOSE).
**Confidence:** high (fetched and decoded). **Re-verify:** the title template
per session — `26JUL19`'s VOD is titled "July 19**th**, 2026", and YouTube
surfaces one video under several headline titles, so match on video **id**
plus duration, never on an exact title string.

## Q-WNT-PACK — ⛔ the WNT pack is AT the broadcast (2026-07-30, 6 sessions) (lane removed 2026-08-12)

**Claim:** The traders already in `KXWORLDNEWSMENTION` react to a word within a
few seconds of it being said. Measured **offset −6.31 s (median)**, range
**−10.83 s … −2.16 s**, over **6 independently resolvable sessions**. A pack
watching a 30–90 s delayed live-TV stream would land at **+30…+90 s**. It does
not. **No obtainable internet feed can win this race.**

| session | offset | spread | slope | strikes | programme span |
|---|---|---|---|---|---|
| 26JUL07 | −4.71 s | 5.23 s | 0.986 | 3 | 355 s |
| 26JUL08 | −7.16 s | 1.85 s | 1.004 | 4 | 241 s |
| 26JUL09 | −6.70 s | 1.87 s | 1.001 | 5 | 905 s |
| 26JUL21 | −10.83 s | 16.61 s | 1.014 | 4 | 764 s |
| 26JUL23 | −2.16 s | 2.46 s | 1.000 | 4 | 912 s |
| 26JUL29 | −6.31 s | 3.67 s | 1.001 | 5 | 748 s |

**Source:** `scripts/wnt-pack-lag.py`, joining two free public legs — word
offsets from the broadcast's own YouTube caption track (Q-WNT-VOD) against
sweep onsets from `GET /trade-api/v2/markets/trades`. Full output in
`state/wnt-pack-lag.json`. No live capture, no subscription, no hardware.
**Method:** least-squares fit of `sweep_wall = slope × programme_offset +
offset` over first-utterance strikes. **Slope is the alignment check** — a
wrong VOD, a mis-parsed caption track, or news-driven repricing all break it.
Slopes came out 0.986–1.014 on the six that resolved.

⚠ **`offset` is a SUM, not a lag.** It is the pack's reaction time PLUS however
far the VOD's `t=0` sits from 22:30:00Z, and this data cannot separate them.
The consistent negative sign across six sessions says the VOD's first frame
aired a few seconds *before* 22:30:00Z — not that anyone traded before the
words. The decision does not depend on splitting them: either way the total is
single-digit seconds, an order of magnitude below any streaming route.

**Implication — what this closes and what it opens:**

1. ⛔ **The live-race lane on this market is CLOSED to us.** Combined with
   Q-WNT-CLOSE (the book is swept in under 1 s), a 30–90 s feed does not arrive
   late, it arrives after the trade no longer exists. **Do not buy a live-TV
   subscription for this market** — it cannot win, and this pin is the reason.
2. ✅ What survives needs no speed: the **count strikes** (`-TRUM` 5+, `-IRAN`
   3+) and **pre-broadcast pricing**. On the latter the tape is emphatic —
   **26 of 47 strike-notes across the 16 sessions are `pre-priced`**, i.e. the
   strike already sat ≥85¢ before air. The board is largely decided by the news
   day, not by the newscast.

**⚠ The pre-registered ad model was REFUTED, and the refutation is the finding.**
This was designed assuming the VOD had commercials removed, so `delta` would be
`pack_lag + accumulated ad time` and would climb through each session; the
estimator was to be `min(delta)`. On 26JUL29 delta came out FLAT
(−6.75, −6.06, −4.06, −3.70, −7.37 s) instead of climbing. Direct check of the
caption timeline: **only 8.5 s of inter-word gaps >3 s in 1206 s** — the VOD is
continuous speech with no ad breaks in it, and wall-clock sweep spacing tracks
programme spacing **1:1 (0.999 over 748 s)**. The slope fit replaced the
minimum, and the refuted premise is recorded here rather than quietly dropped.

**Confidence:** medium-high. Six sessions agree to within ±4 s of the median and
every one carries an independent slope check. **The residual uncertainty is the
`t=0` conflation above, which cannot flip the verdict** — closing it would need
an independent anchor on when the VOD's first frame aired.
**Re-verify:** 10 of 16 sessions REFUSED and the reasons are visible: 6 for
fewer than 3 first-utterance strikes (most strikes are pre-priced), 4 for a
slope off 1.0 (`26JUL25` came out −1.491). Re-run monthly; a drift of the median
toward +30 s would mean the pack moved to a delayed feed and the lane reopened.

## Q-WNT-RECALL — detector accuracy on the REAL broadcast (2026-07-30) (lane removed 2026-08-12)

**Claim:** On 20.1 min of the actual 2026-07-29 WNT broadcast, `streaming_ctc`
at `chunk_ms=80`, Confirmed tier, scores **recall 0.765 (13/17)**,
**precision 0.650 (13/20)**, **20.9 FA/h**. This **replaces the retracted 33 %**
figure, which was measured on ABC News Live — the wrong programme (Q-WNT-SRC).

| phrase | tp | miss | fa | recall | precision |
|---|---|---|---|---|---|
| `oil` | 4 | 0 | 0 | 1.00 | 1.00 |
| `fifth` | 5 | 0 | 1 | 1.00 | 0.83 |
| `blanche` | 3 | 0 | 3 | 1.00 | 0.50 |
| `maintain` | 1 | 0 | 1 | 1.00 | 0.50 |
| `iraq` | 0 | 3 | 0 | **0.00** | — |
| `midterm` | 0 | 1 | 0 | **0.00** | — |
| `ice` | 0 | 0 | 2 | — | **0.00** |

**Source:** `earshot bench-detector --config
configs/webcast-abcwnt-26jul29-replay.toml --engines streaming_ctc --chunk-ms 80
--split dev`, against `corpus/manifests/WNTVOD-26JUL29.toml` built by
`manifest-from-words.py` from the broadcast's own caption track (Q-WNT-VOD).

**Interleaved against the TTS study — the TTS verdicts held on every word where
both had evidence:**

| word | TTS verdict (studio speech) | real broadcast | agrees? |
|---|---|---|---|
| `iraq` | NO-EVIDENCE — piece `Q` logP −10.46, never emitted, unreachable at any threshold | 0/3 recall | ✅ CONFIRMED |
| `midterm` | WEAK | 0/1 recall | ✅ CONFIRMED |
| `blanche` | fires 0.84–0.95, every piece p≈0.99 | 3/3 recall | ✅ CONFIRMED |
| `ice` | 2-piece word that already false-fires; recall would be bought with errors | 0 true occurrences, **2 FA** | ✅ CONFIRMED |
| `tariff` | NEAR-MISS, short by 0.026 | **not said** (settled NO) | ⊘ **NOT RESOLVABLE** |

So the studio-TTS screen is predictive of live-broadcast behaviour, and `iraq`
is dead on real audio exactly as the per-piece autopsy said it would be — the
one thing the 2026-07-29 session was supposed to establish and could not.

⚠ **Two limits, stated because the number is quotable and small-sample.**
(1) **n = 1 session, 17 occurrences.** `maintain` and `midterm` rest on a single
occurrence each; do not quote their per-word recall as a rate. The pooled 0.765
is the defensible figure and it is still one broadcast.
(2) **Ground truth is a caption track, not a human transcript.** It carries ASR
spellings, and `blanche` required an explicit ground-truth-only correction —
the captions write "Blanch", so without it three real utterances would have been
scored as three false alarms instead of 3/3 recall. Any future word may need the
same treatment; a silently-empty phrase in a manifest is the tell.
**Confidence:** medium (measured, but one session). **Re-verify:** run across
the remaining 15 banked sessions in `corpus/wnt/` — that needs the audio, which
`--no-audio` skipped during the 2026-07-30 backfill, and it is CPU-heavy enough
to keep off an event day.

---

## Q-UNOPENED — pre-open events ARE queryable (2026-07-30)

**Claim:** `GET /trade-api/v2/events?status=unopened` is a valid, distinct
listing state: it returns events whose markets have not opened yet, including
Mentions-family events, and an invalid status value is rejected with 400 (so
`unopened` is a real server-side filter, not silently ignored).
**Source:** `GET https://api.elections.kalshi.com/trade-api/v2/events?status=unopened&limit=200`
(live prod, 2026-07-30 15:44Z); `status=bogus` → HTTP 400.
**Evidence:** 200 events returned; among them `KXDEBATEMENTION-26AUG03` and
`KXDEBATEMENTION-26AUG03B` ("What will the candidates say during the …
debate?") — mention events listed before opening.
**Implication:** the discovery sweep's `status=open`-only queries
(`scripts/discover-events.py`) are structurally blind to listed-but-unopened
events; sweeping BOTH statuses closes the class. Both sweeps share this filter
today, so `sweep_delta` cannot detect the blindness — the fix must change both.
**Confidence:** high.
**Re-verify:** if a listed-same-day market is ever missed again, check whether
Kalshi added further status values (`unopened` was undocumented in our pins
until today).

---

## Q-CENSUS — Mentions-series taxonomy is inconsistent; prefixes are unsafe (2026-07-30)

**Claim:** The Mentions category (398 series live today) has **two earnings
families** — `KXEARNINGSMENTION<T>` (current) and `KXMENTIONEARN<T>` (~14
series) — plus typo series (`KXEARNIGNSMENTIONJPM`, and `KXEARNINGSMENTIONSYUM`
which has **zero events**), and non-ticker company identifiers
(`…COINBASE`, `…ALIBABA`, `…DOLLARGENERAL`, `…DEER`, `…ADOBE`, `…DOCUSIGN`).
The `KXMENTIONEARN*` family is **not dead**: `KXMENTIONEARNDAL-26JUN30` was
listed while `KXEARNINGSMENTIONDAL` also exists and got `-26JUL10`. Kalshi
flips families for the same issuer within weeks.
**Source:** `GET /series?category=Mentions` (full ticker list, 2026-07-30);
`GET /events?series_ticker=KXMENTIONEARN{MU,NKE,DAL,FDX,LULU}` — latest events
`26MAR18 / 26MAR31 / 26JUN30 / 26MAR19 / 26MAR17` respectively.
**Evidence (RACE, the non-miss):** `KXEARNINGSMENTIONRACE-26JUL30` markets all
carry `open: 2026-07-23T20:22:00Z` — listed a full week early; every nightly
sweep from 07-23 had it, and the committed slate did too. The 07-30 loss was
launch-side, not discovery.
**Implication:** any prefix- or ticker-pattern-based discovery WILL miss
series. Discovery must sweep the whole category and classify, with delta
alerts for never-seen series (`NEW-FAMILY`) and out-of-prefix series gaining
events (`OUT-OF-PREFIX-OPEN`) — `scripts/mentions-census.py`.
**Confidence:** high.
**Re-verify:** census baseline diff weekly (automated); this pin's counts go
stale by design, the mechanism does not.

---

## Q-PM — Polymarket runs the same mention-market class; read APIs are public (2026-07-30) (lane removed 2026-08-12)

**Claim:** Polymarket lists the identical event class under tag
`mention-markets` (21 open events on 2026-07-30), **including the same
AAPL/AMZN 26JUL30 earnings calls we arm on Kalshi**, at volumes 10–100×
Kalshi's mention boards (closed comparators: Powell presser mention events
$0.9–5.5M, NFLX earnings-mention $6.6M, Trump events $1M+; AAPL/AMZN open
events ~$16–19K pre-call). Keyword overlap on AAPL 26JUL30 ≈ 11/18 of the
Kalshi board (Siri, Gemini, Liquid Glass, iPhone 18, Tariff, Friday Night
Baseball, Creator Studio, Streaming, Sport, Widget, Apple Pay). Two market
shapes: single-mention (our class) and cumulative-count ("say AWS 20+ times"),
the latter re-pricing on every utterance — a live streaming counter holds edge
for the whole call, not one race.
**Source:** `GET https://gamma-api.polymarket.com/events?closed=false&tag_slug=mention-markets`
· `GET https://gamma-api.polymarket.com/public-search?q=…` (live, 2026-07-30).
**Claim (transport):** `gamma-api.polymarket.com` and
`data-api.polymarket.com` accept python/urllib with a browser User-Agent, but
**`clob.polymarket.com` TLS-fingerprints the client**: python's TLS stack gets
HTTP 403 regardless of User-Agent while curl with the same UA passes. All CLOB
market-data reads are public (no auth, no key): `GET /book?token_id=`,
`GET /midpoint?token_id=`, `GET /prices-history?market=&interval=&fidelity=`
(~50–90 s granularity), and batched `POST /books` with body
`[{"token_id":…},…]` (94 tokens in one call verified). Book responses carry a
**millisecond server `timestamp`** and a `hash` per book (dedupe key), and
**both sides sort best-price-LAST** (bids ascending, asks descending —
verified live: best bid 0.943 = `bids[-1]`, best ask 0.998 = `asks[-1]`).
Trades: `GET https://data-api.polymarket.com/trades?market=<conditionId>[,<conditionId>…]&limit=N`
is public, second-granularity, accepts a comma-separated market list;
`eventSlug=` is **silently ignored** (returns the global feed — do not use).
Token ids come from Gamma `markets[].clobTokenIds` (JSON-encoded string).
**Evidence:** all endpoints exercised live 2026-07-30 15:4x Z from CDMX;
`scripts/pm-capture.py` (curl-subprocess transport for exactly this reason)
ran 15 s locally: 116 book rows + 200 trades, 0 errors; deployed to the box
(us-east-1) capturing AAPL+AMZN from 15:49Z.
**Implication:** the measure-only leg needs zero auth, zero wallet, zero src/
changes. Execution (CLOB orders = EIP-712-signed, USDC on Polygon, separate
credential world) stays out of scope until the edge report justifies it.
**Confidence:** high on everything exercised; medium on rate limits (none hit
at ~1.1 req/s sustained).
**Re-verify:** TLS-fingerprint behavior on any capture failure (Cloudflare
rules change without notice); volumes per event at arm time.

### Q-PM AMENDMENT — the 10–100× volume claim is FALSE for earnings-mention (2026-08-04)

**Claim:** the "PM runs this class at 10–100× Kalshi volume" line above holds
for the Powell / Trump / NFLX boards it was sampled from. It is **inverted**
for earnings-mention boards, which is the class we actually arm. Measured from
the tapes (not by hand — `scripts/xvenue-report.py`, artifacts in
`state/xvenue/*/report.json`):

| pair | Kalshi trades | PM trades | Kalshi notional | PM notional | ratio |
|---|---:|---:|---:|---:|---:|
| `pltr-26aug03` | 7,918 | 48 | $103,273.07 | $2,160.70 | **47.8×** |
| `amd-26aug04`  | 4,903 | 14 | $97,893.55 | $335.22 | **292.0×** |

Kalshi is the deeper and far tighter venue here (PM median spread 31¢ on PLTR,
19¢ on AMD). PM trade counts are YES-leg only; the postmortem's "121" pooled
both legs. Kalshi traded size reproduces the hand-typed postmortem figure
exactly (224,142.32 contracts), which is what validates the chain.
**Implication:** a PM price move is not a usable Kalshi signal on this class at
these print rates. The Δt axis should be prioritized on `KXFEDMENTION`, where
PM has the depth the original claim was sampled from.
**Source:** `docs/history/POSTMORTEM-26AUG03.md` §Polymarket (first, by hand);
reproduced from tape 2026-08-04. **Confidence:** high (n=2 events).

---

## Q-XVENUE-WS — Polymarket pushes a millisecond book feed (2026-08-04, live) (lane removed 2026-08-12)

**Claim:** Polymarket exposes a market-data **WebSocket**, so the cross-venue
lane no longer has to poll one venue while the other pushes.

- Endpoint `wss://ws-subscriptions-clob.polymarket.com/ws/market`. The host is
  **`ws-subscriptions-clob`** — `ws-subscribe-clob.polymarket.com` does not
  resolve (NXDOMAIN), and `clob.polymarket.com/ws/market` is a 404.
- **Python's own TLS stack completes the handshake here** (HTTP 101). The
  fingerprint wall pinned in Q-PM applies to `clob.polymarket.com`, not to this
  host, so `pm-tape.py` needs no curl subprocess.
- Subscribe frame: `{"assets_ids":[…],"type":"market"}`.
- Delivers `book` (full snapshot: `bids`/`asks`/`hash`/`tick_size`/
  `last_trade_price`) and `price_change` (deltas carrying `best_bid`/`best_ask`
  inline), both with a **millisecond string `timestamp`**.
- **`tick_size` is 0.001.** Polymarket prices a tenth of a cent — which is why
  the canonical cross-venue schema is micro-dollars (`*_e6`) and NOT cents; a
  cents representation silently destroys real price motion on this venue.
- A connection **survived 100 s with no client PING** while frames kept
  arriving (docs suggest PING every 10 s). `pm-tape.py` pings at 10 s anyway;
  behaviour beyond 100 s is untested.
- The newer `ws-live-data.polymarket.com` service answers CLOB topics with
  *"CLOB messages are not supported anymore"*; its `activity/trades` topic IS
  live (565 frames in 20 s, global firehose). Unknown topics fail loudly
  (`leger GetTopics error … NotFound`), which is how the live set was found.

**Evidence:** exercised live 2026-08-04 22:3x–22:4xZ from CDMX;
`scripts/pm-tape.py` recorded 479 rows / 40 s over 32 AMD tokens, 0 errors,
0 reconnects.
**Implication:** both venues are now `feed_kind: "push"`, so a lead/lag figure
is no longer biased by up to a full second toward the pushed side. This is the
single change that makes a sub-second cross-venue Δt measurable at all.
**Confidence:** high on everything exercised; medium on long-session stability
(100 s observed).
**Re-verify:** the handshake, on any capture failure — `scripts/xvenue-daily.sh`
does this every day and reports a FINDING if it stops upgrading.

---

## Q-XTAPE — Kalshi wire facts the cross-venue recorder had to pin (2026-08-04, live)

**Claim (WS carries no server clock):** `orderbook_snapshot` / `orderbook_delta`
frames carry `seq` but **no server timestamp**. A Kalshi-side time axis
therefore has to be our own receive stamp, which is why the canonical schema
makes `recv_wall_ns` primary and states `venue_ts_ns: null` rather than
substituting one clock for the other. Both feeds must run on ONE host or the
measurement is our network asymmetry.
**Claim (empty boards serialize empty):** a mention board with no resting
orders sends snapshots whose `msg` carries only `market_id`/`market_ticker` —
no `yes_dollars_fp`/`no_dollars_fp` keys at all. Observed on the whole AMD
26AUG04 board pre-call. Not a parse failure; a genuinely empty book.
**Claim (public trades tape is MICROSECOND, and sizes are `count_fp`):**
`GET /markets/trades` returns `created_time` as
`"2026-08-03T21:41:53.929037Z"` — microsecond precision, **not** whole seconds.
Size is **`count_fp`** (fixed-point, fractional contracts); the plain `count`
key is absent, and reading it yields a silent zero notional.
**Implication:** the "~2 s joint quantisation floor" that
`scripts/pm-edge-report.py` documented was only ever true of the **Polymarket
`data-api` trades** feed; the Kalshi side was never the binding constraint.
`scripts/xvenue-normalize.py` recorded each row's own
`venue_ts_granularity_ns`, so the floor was computed per comparison instead of
assumed. (Both scripts left with the expansion lane 2026-08-12; the recorder
`examples/book-tape.rs` and this pin's wire facts remain live.)
**Evidence:** `examples/book-tape.rs` live against
`KXEARNINGSMENTIONAMD-26AUG04` (17 markets, 0 dropped) and three liquid boards
(2,299 frames / 25 s → 2,145 two-sided quotes, 0 violating
`0 < bid < ask <= $1`); `state/KXEARNINGSMENTIONPLTR-26AUG03/tape-2026-08-03.json`.
**Confidence:** high.

---

## Q-EDGAR — SEC full-text search carries earnings dial-ins (2026-07-30)

**Claim:** SEC EDGAR full-text search is unauthenticated, effectively
unthrottled at our volumes, and returns the earnings press-release exhibit
carrying BOTH the conference dial-in number(s) AND the passcode. It is the only
automatable source for the number hunt.
**Source:** `GET https://efts.sec.gov/LATEST/search-index?q=<phrase>&forms=8-K&ciks=<10-digit>&startdt=&enddt=`
then `GET https://www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dashes>/<file>`;
ticker→CIK from `https://www.sec.gov/files/company_tickers.json` (10,429 rows).
All live prod, 2026-07-30.
**Evidence (4/4 control cases reproduced verbatim by `scripts/hunt-dial-in.py`):**
- CMG `888-317-6003` + intl `412-317-6061`, code `3937444` — matches the Q-ADMIT
  pin exactly (`cmg-20260729xex991.htm`).
- YUM `800-715-9871` + intl `646-307-1963`, conference ID `8719077` — matches
  the registry entry exactly (`a8kex9917302026.htm`).
- WEN `844-200-6205` + `929-526-1599`, **event ID** `280384`.
- F: **zero** dial-in filings in the scanned window — its last was 2024-04-24,
  which dates the webcast-only migration rather than leaving it unresolved.

**Wire facts that cost real numbers before they were fixed:**
- A descriptive `User-Agent` is **mandatory** — empty UA is a hard **403**. The
  legacy `www.sec.gov/cgi-bin/srqsb` endpoint is dead (404).
- Numbers match **literally**: `"8883176003"` → **0 hits**; `"888-317-6003"` →
  3,234. Never query an unformatted number.
- A bridge number is **not an issuer key** — `888-317-6003` is a generic Chorus
  Call DID shared across hundreds of filers. Always scope by `ciks` + dates.
- **The passcode is the high-precision key**: `"3937444"` → 3 hits in all of
  EDGAR; `"8719077"` → 4. Use it to confirm and dedup.
- Rate limits: 15 sequential and 20 concurrent requests all returned 200, no
  429. SEC publishes a 10 req/s ceiling; we pace at 0.25 s on principle.

**Extraction facts (measured on the filings themselves):**
- Separator vocabulary matters: YUM writes `800/715-9871`. A `[\s.-]` class
  silently dropped its North America bridge while keeping the international one.
- **Replay dominates, it does not compete**: *"To access the playback, dial
  +1/800-770-2030"* puts a bridge cue one char from the number and the replay
  cue 14 chars out — nearest-cue alone promoted BOTH YUM playback lines to live
  bridges. A replay cue in the preceding ~140 chars now governs the clause.
- Label vocabulary matters: WEN writes `event ID`, not `conference ID`. A bridge
  extracted without its code is an unenterable lane that reports as a success.
- Media-contact numbers share the paragraph with the bridge (CMG's EX-99.1 has
  `(949) 524-4035` alongside it), so classification is by NEAREST cue.

**Implication:** the hunt is automatable for 8-K filers. It is NOT automatable
for issuers who publish only on an IR page (AAPL is the type specimen — its
8-Ks carry no dial-in), because those pages are Cloudflare bot-challenged
**identically from the laptop and the box** — a challenge, not a geo-block, so
relocating the fetcher cannot fix it. Foreign private issuers file 6-K and are
not yet covered.
⚠ **A number is still not admission** (THE MSFT RULE) — only a probe dial can
establish what the bridge does to a silent caller.
**Confidence:** high (4/4 controls, 32-issuer live sweep).
**Re-verify:** if SEC changes the FTS endpoint or begins enforcing 10 req/s.

## Q-STALE-EVENT — one event ticker per series, and `sub_title` goes stale (2026-08-04, live)

**PINNED.** Kalshi reuses a SINGLE event ticker per mention series for the
standing question "*What will <issuer> say during their next earnings call?*"
and does **not** re-date it, or its `sub_title`, when the event rolls forward to
the next quarter's call.

**Claim:** an event returned by `GET /events?status=open` can carry a
`sub_title` date weeks in the past while its markets are live and repricing.
The date is stale; the event is not over.

**Source / Evidence** (read-only public GETs, 2026-08-04 ~14:5x–15:2xZ):

- `GET /events?series_ticker=KXEARNINGSMENTIONPGR` returns exactly two events,
  `KXEARNINGSMENTIONPGR-26JUL15` and `-26APR15`, both titled "What will
  Progressive say during their **next** earnings call?"
- `KXEARNINGSMENTIONPGR-26JUL15` carries `sub_title = "On Jul 15, 2026"` and
  `call_date_source = "sub_title"` in `slate/latest.json` (generated 02:02Z).
- Its 12 markets were `status=active` with `close_time 2026-12-31T15:00:00Z`.
- `GET /markets/trades?ticker=KXEARNINGSMENTIONPGR-26JUL15-SNAP` returned
  trades at **14:33:26Z … 14:54:09Z on 2026-08-04** — i.e. during Progressive's
  Aug 4 call, 20 days after the `sub_title` date.
- Board totals that day: 12 markets, ~178,000 contracts, settling to
  0.99/0.02.

**Implication:** `today <= call_date` is not a safe way to drop an event, and
this is the THIRD distinct Kalshi misdating class after BA (ticker and
`sub_title` disagree) and HOOD (both wrong together). Note `ticker_misleads()`
is structurally blind here for the HOOD reason — ticker `26JUL15` agrees with
the stale `sub_title`. The free discriminator is that both sweeps query
`status=open`, so an open event with a passed date is self-refuting.
`scripts/discover-events.py` now emits `stale_open_event` and reports it
outside the horizon filter; `recon-worklist.py` surfaces `STALE-DATE`.

**Cost of not having this:** Progressive was absent from `slate/worklist.md`
entirely on 2026-08-04. We recorded 4,121 s of that call to WAV and placed zero
orders on a board that traded ~178,000 contracts.

**Confidence:** high (direct trade timestamps against a stale `sub_title`).
A live sweep the same day flagged a second instance unprompted — `BULL`
(Webull) `-26JUL24`, 17 markets open and **unresolved** (0.15–0.58), i.e. a
future call we had also been blind to.
**Re-verify:** if Kalshi begins minting per-call event tickers, or if
`sub_title` starts tracking the rolled-forward date.

## Q-ADMIT addendum — SPOT and PGR admission (2026-08-04, live)

**PINNED.** Two new `held` venues, both established by probe dial per THE MSFT
RULE, taking probed `held` coverage from 1 venue (CMG) to 3.

- **SPOT (Spotify).** Toll-free + numeric PIN, dialled 12:02:57Z. Held
  **598.4 s** with **11 stalls >100 ms** — clean. DTMF accepted first try. The
  transcript is unambiguously the live Q2 2026 call (operator preamble → Brian
  Goldberg → both co-CEOs → Slido instructions). Capture ended on OUR 600 s
  probe cap (`capture_bakeoff: max duration reached — stopping max_secs=600`),
  **not** a bridge drop, cutting off mid-sentence before Q&A.
  Evidence: `corpus/bridge-probes/spot_2026_08_04.{json,jsonl,wav}`.
- **PGR (Progressive).** Toll-free + dial-in PIN, six consecutive full legs
  from 14:01Z (598.1 / 598.4 / 599.0 / 598.5 / 598.9 / 529.9 s), each
  re-dialling with DTMF and each ending on the same 600 s cap. Arrival jitter
  was high (91–300 stalls/leg, max 4.5 s) via ngrok on the laptop, but frame
  count matched wall-clock in every leg, so **no audio was lost** — the stalls
  are delivery jitter, not gaps in the call.
  Evidence: `corpus/bridge-probes/pgr_2026_08_04_leg0*.{json,jsonl,wav}`.

**Implication:** neither venue was in `configs/dial-ins.toml` at all, so
`slate-stage.sh:291` (`PHONE_OK=1` iff `admission == "held"`) staged
webcast-only and no phone arm was ever derived for either. Both are now
registered, with digits in `state/hunt/numbers-probed.env` (gitignored, 0600).

**Confidence:** high (mechanical hold times + intelligible transcripts).
**Re-verify:** next quarter — these are conference-provider reservations and
digits rotate; `admission_probed` goes stale at 30 days.

### Amendment — the live `--promote` path (2026-08-04, live)

**PINNED.** The one unproven leg of the SPOT fix — `bridge-probe.sh --promote`
wiring end-to-end with a real call — was rehearsed against the TTS loopback
(+17627212978, the Q-REDIAL rehearsal number):

- One dial, `--secs 120 --promote --issuer TTSREHEARSAL`: held **114.6 s**
  (5744 frames, 16 stalls) — above the ≥95% auto-write floor (114.0 s).
- `registry-admit.py --auto` made the write itself: printed
  `created [TTSREHEARSAL] admission=held probed=2026-08-04`, and `git diff`
  showed **exactly one appended 14-line section at EOF, zero other hunks** —
  THE MSFT RULE comments, `[HOOD]`'s triple-quoted notes, and every other
  section byte-identical. `tomllib` parsed the result. The proven number
  landed in `state/hunt/numbers-probed.env` (0600).
- The test section was then reverted (`git checkout -- configs/dial-ins.toml`
  + the two env lines hand-removed); the artifacts below are the durable
  record.

Evidence: `corpus/bridge-probes/ttsrehearsal_2026_08_04.{json,jsonl,txt,wav}`.
Also live-validated the same day, in scratch, against the real board:
`discover-events.py` surfaced `KXEARNINGSMENTIONBULL-26JUL24` with
`stale_open_event: true` (past-dated ticker, board open — the PGR class) and
`recon-worklist.py` emitted its `STALE-DATE(open, kalshi=2026-07-24)` escalate
row; `board-open.py --issuer BULL` agreed (exit 0).

**Confidence:** high. **Re-verify:** after any change to `bridge-probe.sh`'s
promote block or `registry-admit.py`'s write path.

### Q-ADMIT addendum — TLN, and the two probes that meant opposite things (2026-08-05, live)

**PINNED.** Talen Energy is the type specimen for why a reservation-window
bridge needs an IN-WINDOW probe: the same number, the same PIN, dialled twice on
the same day, produced one non-verdict and one clean `held`.

- **17:21Z (T-3h09m, OUTSIDE the T-90..T-30 window) — CLASSIFIES NOTHING.**
  The call connected and held 13.1 s / 671 frames, and the bridge said:
  *"Please enter your dial-in pin and press pound when finished. The conference
  is not yet available. Please reconnect closer to the scheduled start time."*
  That is a reservation-window venue behaving correctly. Recording it as
  `dropped` would have permanently failed the admission gate on false evidence,
  and `dropped` is not re-testable by a later probe — the registry would simply
  say the bridge hangs up on us.
- **19:55Z (T-35, IN-WINDOW) — `held`.** The bridge issued the same PIN prompt
  and then held for **119.0 s of a 120 s cap**. Capture ended on OUR cap, not a
  bridge drop.

**A bridge that ANSWERS while unprovisioned is the sharp edge.** The 17:21Z leg
looked *successful* to Twilio — call answered, media forked, 671 frames — and
then died at 13.1 s. A too-early unattended dial therefore consumes an attempt
and reports success. What carries across the provisioning moment is dial
attempts plus mid-call reconnects, which is why the phone-lane env now ships
`DIAL_ATTEMPTS=5` and `STREAM_CONFIRM_SECS=150` against launch-event.sh's
defaults of 3 and 90.

**`--promote` did not fire, and the failure was silent.** `registry-admit.py`
hit a `tomllib` ImportError under launchd's minimal PATH, so the `held` verdict
had to be written by hand from the probe artifact. The laptop interpreter split
is the cause: homebrew `python3` 3.14.6 has `tomllib` but a broken `plistlib`
(pyexpat cannot resolve against the system libexpat), and `/usr/bin/python3`
3.9.6 is the exact inverse — it predates `tomllib` entirely. **Pin the
interpreter explicitly in any scheduled context.** The us-east-1 box does not
have this split (one `python3` 3.12.3 with both modules), which
`scripts/box-ready.sh` now verifies under the systemd user PATH rather than the
login shell.

**Implication:** read the REGISTRY after a promote, never stdout — which is what
`scripts/tln-phone-golive.sh` did, and why the verdict survived at all.
**Evidence:** `corpus/bridge-probes/tln_2026_08_05.{json,jsonl,wav}`;
`configs/dial-ins.toml` `[TLN]`.
**Confidence:** high. **Re-verify:** admission is stale after 30 days; the
window class is a per-venue fact that does not expire.

---

## Q-BOX — us-east-1 execution-host readiness, measured (2026-08-05, live)

**PINNED.** Read-only SSH survey of `ubuntu@3.212.174.94` at **21:15Z**, four
hours before this entry was written and after the TLN call it explains. The box
was NOT down — and was still not runnable.

| fact | measured | consequence |
|---|---|---|
| `~/ear/src/risk.rs` | `MENTION_FILL_CAP_CENTS = 50` | laptop tree was 97; `mention_fill_gate` refuses any higher cap |
| `~/ear/target/release/earshot` | built **2026-08-04 19:20:37Z**, sha256 `3d139aeb17441a95…` | older than `src/kalshi/book.rs` — the binary does not contain the tree |
| `/etc/caddy/Caddyfile` routes | `/e/8080`…`/e/8085`, `/e/8099` **only** | TLN's config bound **8093**; every other path hits `handle { respond 404 }` |
| `systemd --user` PATH | `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin` | **no `~/.cargo/bin`** — a fire-time compile is impossible; `shasum` and `timeout` ARE present |
| `python3` under that PATH | 3.12.3, `tomllib` OK | the interpreter split is a LAPTOP problem, not a box one |
| `~/ear/state/hunt/*.env` | absent | `deploy-box.sh` excludes `state/` — digits never arrive by deploy |
| lingering / NTP / TLS | `Linger=yes`; NTP active+synced, 1 s skew; cert to Oct 28 2026 | fine |
| disk | 5.4 GiB free (81 % used), no `target/debug` to reclaim | preflight's ≥5 GiB gate passes barely; `target/release` is 1.6 GiB and must not be deleted |
| host | c6a.4xlarge, 16 cores, load1 0.00, up 7 d | idle throughout the call it could have run |

**Two independent blockers, not one.** The stale binary is the one that was
found at T-60 on 26AUG05. The unrouted bind port was found only in hindsight:
even with a fresh binary, `wss://media.3-212-174-94.sslip.io/e/8093` would have
hit Caddy's 404 catch-all and no media would ever have arrived. The cause is a
disagreement nothing checked — `scripts/slate-stage.sh` allocates `BIND_PORT`
monotonically across the whole slate (it had reached 8103 by 26AUG07) while the
ingress routed six ports. `deploy/caddy/Caddyfile` now enumerates 8080–8098.

**Implication:** host readiness is a prep-time artifact, not a T-60 discovery.
`scripts/box-ready.sh` produces it (`state/<EVENT_ID>/box-ready.json`, schema 1)
and exits nonzero on NO-GO. Run against this config on 2026-08-05 it returns
NO-GO with 5 blockers, reproducing both of the above.
**Re-verify:** after any deploy, any `deploy/caddy/Caddyfile` change (the box
copy must be re-rendered by `scripts/install-tls-ingress.sh`), any instance or
EIP move (the sslip.io hostname encodes the IP), and before every phone event.

---

## Q-CALLPATH — first-dial retry + no-stream watchdog (SCHEDULED, not yet live)

**NOT YET PINNED — stub-tested, live rehearsal pending.** (2026-08-05: the
phone-lane event env now ships `DIAL_ATTEMPTS=5` / `STREAM_CONFIRM_SECS=150`
and `scripts/event-autorun.sh` forwards them, so the first box-hosted phone
arm IS this pin's live evidence — record it there, not here, until it runs.) The call-path
hardening (2026-08-04, commit on webcast-lane) is proven by
`tests/redial_supervisor.rs` (10), `tests/launch_event_posture.rs` (+6), and
`tests/twilio_source.rs` (+3), all under bash 3.2 and 5. Per the CLAUDE.md
unattended rule these count as validated only after ONE combined live
rehearsal with the production mechanism — deferred to after tonight's webcast
launches (tonight is webcast-only, so `launch-event.sh` is not exercised
live until then). Tonight's events do NOT ride this unproven-live path.

Recipe (on the box, one TTS call chain vs +17627212978):
1. `configs/rehearsal-qcallpath.toml` = clone of `rehearsal-qredial.toml`
   with `max_reconnects = 3`, `idle_timeout_secs = 8`; pin the binary via
   `preflight-event.sh`.
2. Offline gates first (no call): launcher REFUSES a config with
   `max_reconnects` stripped, accepts it with `--one-shot-ok`;
   `preflight-event.sh` FAILs with no TLS env; pinned `arm` with
   `EARSHOT_REDIAL_DIR` unset refuses at load (Gap E).
3. `dial-fail-once.sh` wrapper: first invocation exits 5, second rewrites
   `--stream-url` to a dead-but-routed path (`wss://<host>/e/9999`, Caddy
   404s → stream never lands), later invocations `exec twilio-dial.sh`.
4. `launch-event.sh --config rehearsal-qcallpath.toml … --to +17627212978
   --tls-host media.<ip>.sslip.io --dial-script <dial-fail-once.sh>
   --stream-confirm-secs 20`.
5. Observe: attempt 1 exits 5 → retry → attempt 2's stream never lands → NO
   STREAM page + synthetic `no_stream` marker → supervisor hangs up the deaf
   CallSid + redials (correct URL) → `stream.connected` confirms. Then a real
   `twilio-hangup.sh $(cat call.sid)` mid-call → Q-REDIAL parity
   (drop→new CallSid <2s, fresh audio <6s). Then real `SIGTERM` → clean
   report, ledger 0¢, no orphan pids.

Pass criteria to pin here once run: retry within 10 s of the induced
transient; NO-STREAM page ≤ confirm+5 s with the deaf sid; deaf call hung up;
mid-call parity holds; one-shot no-double-fire across every splice; SIGTERM
clean.

## Q-GRADE — missed-arm reconciliation, launchd-fired (2026-08-04, live)

**PINNED.** `com.earshot.missed-arm` (17:30 local daily →
`scripts/missed-arm-daily.sh`) validated per the CLAUDE.md unattended rule,
NOT by a manual run:

- `launchctl bootstrap` + `kickstart gui/<uid>/com.earshot.missed-arm`
  (launchd env): exit 1 with both real positives — PGR (4,121 s captured,
  0 orders, 178,691 contracts traded) and SPOT (598 s, 0 orders, 157,217) —
  and the alert path fired with no `warn: notify.sh failed` in the log.
- Second kickstart + real `SIGTERM` mid-run: wrapper logged `exit=143`, no
  surviving processes, no partial state (the checker is read-only), and a
  killed run pages UNDETERMINED rather than passing silently.

Evidence: `~/Library/Logs/earshot/missed-arm.log` (2026-08-04 17:3xZ entries).
**Caveat:** findings for box-launched events are expected-incomplete until the
box's `state/` is rsynced down — see docs/nodes/control-grade.md §Known gap.
**Re-verify:** after any change to `missed-arm-daily.sh`, the plist, or
`missed-arm-check.py`'s exit contract.

### Ops note — `cargo test` needs `ORT_DYLIB_PATH` when `models/` is present

On a box with `models/` fetched, `tests/sherpa_kws.rs` loads the real model;
with `ORT_DYLIB_PATH` unset the ort loader SIGABRTs (`libc++abi: terminating
due to uncaught exception of type Ort::Exception`) instead of skipping — and
non-deterministically (one same-tree run passed, the next aborted, likely
load-order dependent). Observed live 2026-08-04 on the laptop. Run gates as
`ORT_DYLIB_PATH=vendor/onnxruntime/<platform>/lib/libonnxruntime.dylib cargo
test`, and never judge a suite through `| tail` — the 07-25 masked-exit-code
defect hid exactly this.

## Q-WSTRADE — the Kalshi WS `trade` channel, pinned for the sweep lane (2026-08-07, live)

**Why this had to be pinned before any code.** The sweep lane's trigger is
defined on **taker-YES prints** (`sweep-lane-exploration/lib/wave_ladder.py`:
burst of `taker == "yes"` prints, top ≥ 95¢, size floor). `orderbook_delta` —
the only channel `src/kalshi/book.rs` subscribes to — carries no taker side and
therefore cannot supply that trigger. Line 311 of this file had left channel
enumeration as an open re-verify since M2. Done now.

**Claim:** Five channels subscribe successfully on production
`wss://api.elections.kalshi.com/trade-api/ws/v2` with the standard three signed
headers (Q3 scheme, path `/trade-api/ws/v2`): **`trade`**, **`ticker`**,
**`orderbook_delta`**, **`market_lifecycle_v2`**, **`fill`**. Each returns a
`subscribed` ack with its own `sid`. **`ticker_v2` does NOT exist** — it returns
`{"code": 8, "msg": "Unknown channel name"}`.
**Evidence:** live probe 2026-08-07, 300 highest-open-interest open markets.
**Confidence:** high (observed acks + one negative control).

**Claim:** A `trade` message is
`{"type":"trade","sid":N,"seq":N,"msg":{...}}` and `msg` carries exactly ten
fields, **all present on every message observed**:

```json
{"trade_id": "c72b8e69-…", "market_ticker": "KX…",
 "yes_price_dollars": "0.0090", "no_price_dollars": "0.9910",
 "count_fp": "103.09", "taker_side": "yes",
 "taker_outcome_side": "yes", "taker_book_side": "bid",
 "ts": 1786135600, "ts_ms": 1786135600115}
```

**Implication — the live feed is field-identical to the REST tape the backtest
was built on.** `yes_price_dollars`, `count_fp` and `taker_side` are the three
the trigger reads, named the same as in `GET /markets/trades`, so the Rust port
consumes WS trades with the same semantics as the Python backtest and no
translation layer.
**Confidence:** high. **Re-verify:** if a `trade` field ever arrives absent —
the probe asserted all-ten-on-every-message, so a partial message is a schema
change.

**Claim:** **`count` (non-fp) is absent from the WS `trade` message too**, not
just from the REST tape. Reading it yields a silent zero notional.
**Implication:** Q-XTAPE's warning extends to the live feed. `count_fp` only.
**Confidence:** high.

**Claim:** `ts_ms` gives **millisecond** trade timestamps, against the REST
tape's microsecond `created_time`.
**Implication:** Coarser than the archive, and irrelevant at the sweep lane's
scale — the burst gap is 2.0 s and the freshness gate is 1 s. Do not reuse this
channel for anything needing sub-millisecond Δt; that is what
`examples/book-tape.rs` receive stamps are for.
**Confidence:** high.

**Claim:** The connection drops with `ConnectionClosedError: no close frame`
inside 45 s if the client does not ping, even while subscribed to 300 markets.
**Implication:** the `trade` channel needs the same liveness discipline
`book.rs` already applies to `orderbook_delta` (ping every 500 ms; pongs refresh
`feed_age`, so staleness measures CONNECTION health, not trading activity). A
sweep watcher that inherits `book.rs`'s loop gets this for free; one written
fresh will silently go deaf in a quiet market.
**Confidence:** high (reproduced; the probe sent no pings).

**Not established:** only `taker_side: "yes"` was observed live (3 trades in the
window). `"no"` is inferred from the REST tape's identical field and is NOT
pinned here.

## Q-BOOKFAN — one `orderbook_delta` subscription carries at least 203 markets (2026-08-07, live)

**Why this needed pinning.** The sweep lane's §R8/§R9 book evidence has to be
banked across every live mention market at once, and nothing in
`src/kalshi/book.rs`, `examples/book-tape.rs` or this file stated whether one
subscribe frame has a `market_tickers` ceiling. The harvester's whole shape —
one process vs sharding into N — turns on the answer, and guessing a shard size
would have been an unmeasured constant baked into the design.

**Claim:** A single signed WS subscription to `orderbook_delta` accepts **203
market tickers in one `market_tickers` array** and delivers all of them.
**Evidence:** live 2026-08-07T20:43Z, all 203 open mention markets across 10
series (KXFEDMENTION, KXSECPRESSMENTION, KXTRUMPSAY{,COMPANY,MONTH,NICKNAME},
KXVANCEMENTION, KXFOXNEWSMENTION, KXMAMDANIMENTION, KXMRBEASTMENTION) in one
`record-book.sh --tickers` run: session header `markets=203`, 90 s, **1,730
frames, dropped 0**, `close.reason=deadline`. A 4-hour run at the same width
sustained it (11,038 frames at the 12-minute mark, 0 dropped).
**Confidence:** high (observed, not inferred).
**Does NOT establish:** that 203 is the ceiling — only that the ceiling is
above it. If a future harvest needs the full non-mention board, re-measure
rather than assuming this scales.

**Implication:** the harvester batches into ONE subscription, not one process
per event. Per-event processes would also serialize on the cargo lock, since
`record-book.sh` rebuilds the example on every invocation.

**Claim:** throughput at that width is modest — 203 markets produced ~19
frames/s and ~372 bytes/frame ≈ **21 MB/h uncompressed**.
**Implication:** disk is not the binding constraint for a multi-day harvest;
the earlier 1.5–3 GB/day estimate was extrapolated from a liquid-board ceiling
and does not apply to the current (seasonally thin) mention board.
**Confidence:** medium — one 4-hour sample on a quiet Friday evening; a live
FOMC or a Trump speech will be far busier.

## Q-DISCOVER — enumerate mention markets PER SERIES; paging `status=open` does not work (2026-08-07, live)

**Why this is here.** The sweep lane's `universe.rs` has to answer "which mention
markets are open right now" every refresh, and that answer sets the risk
allowlist. Three attempts at it produced **0, 15, and 0** open mention markets
within minutes of each other, while a concurrent harvester was successfully
subscribed to 203 of them (Q-BOOKFAN). The method was wrong, not the market.

**Claim:** `GET /markets?status=open&limit=1000` **cannot be reliably paged to
exhaustion**. Two identical 200-page runs (200,000 markets each) minutes apart
returned different slices — 15 mention markets, then 0. The cursor does not
converge within any bounded page count on a universe of this size (~90k+ open
markets, dominated by MVE sports legs).
**Implication:** any discovery built on scanning the whole open-market list is
non-deterministic and will silently under-report. Do not write one.
**Confidence:** high (reproduced, contradictory results).

**Claim:** `GET /markets?series_ticker=<S>&status=open&limit=1000`, iterated over
the series in `slate/census.json`, is exact and stable: **398 series queried in
156 s, 0 errors, 653 open mention markets across 36 series** (2026-08-07T21:2xZ).
**Implication:** this is the discovery method. ~400 requests at 10 tokens each is
~4,000 tokens against a Basic budget of 200/s, so a full refresh costs ~20 s of
budget and can run every few minutes without pressure.
**Confidence:** high.

**Claim:** **A substring match on `"MENTION"` is NOT a valid mention filter.**
`KXTRUMPSAY`, `KXTRUMPSAYMONTH`, `KXTRUMPSAYCOMPANY` and `KXTRUMPSAYNICKNAME`
are mention-class series carrying 105 open markets between them, and none
contains the string. Membership in `slate/census.json` is the test.
**Implication:** `sweep-lane-exploration/probe_series.py` is unaffected — its
population came from the census, not a substring — but any new discovery code
that reaches for the obvious substring will silently drop whole venues.
**Confidence:** high.

**Claim:** 653 open markets against Q-BOOKFAN's measured 203-per-subscription
means full coverage needs **at least 4 concurrent `orderbook_delta`
subscriptions**, or a re-measure of the ceiling (Q-BOOKFAN pins 203 as a floor,
not a limit).
**Confidence:** high on the arithmetic; the ceiling itself is unmeasured.

## Q-TAPEORDER — `GET /markets/trades` returns NEWEST-FIRST (2026-08-07, live)

**Why this needed pinning.** It is a silent-corruption fact, not a
documentation gap: a consumer that assumes ascending order gets a plausible
wrong answer rather than an error. It cost a real defect the same day it was
found (see below), and nothing in this file or Q-XTAPE stated it.

**Claim:** The public trade tape arrives **descending by `created_time`**
(newest first). Verified on a banked 242-print market
(`KXEARNINGSMENTIONAAPL-26JUL30-CREA`): `ts == sorted(ts, reverse=True)` is
true, ascending is false. The Python pipeline is safe by construction —
`sweep-lane-exploration/lib/sweep_segment.py::normalize()` sorts — which is
precisely why the ordering had never been noticed.
**Confidence:** high.

**Implication (the defect it caused).** `src/sweep/wave.rs::waves()` segments
bursts with a `saturating_sub` on consecutive timestamps, so a *backwards* gap
evaluates to `0` — "same burst". Fed the raw descending tape, 242 prints
collapsed into **one** wave where the Python found **59**. No panic, no
warning: just a different strategy. Every synthetic fixture happened to be
ascending, so **only the real-wire vectors in `tests/sweep_parity.rs` exposed
it** — the case for testing a port against banked wire data rather than against
fixtures written by the same hand as the code.

**Guard now in place:** `waves()` documents the precondition and carries a
`debug_assert!` on ascending order; the parity test's `to_prints()` mirrors
`normalize()` including its sort; `Tracker` (the live path) drops backwards
prints rather than trusting them, because on a per-market-sequenced WS feed
out-of-order means the feed is broken, not that the data needs re-sorting.
**Re-verify:** if Kalshi ever documents or changes tape ordering — but the guard
holds either way.

## Q-Q4PHONE — every Q4-native webcast has a phone lane, and its Meeting ID is PUBLIC (2026-08-11, live)

**The Q4 attendee REST twin carries a conference-call block that neither
`q4-resolve.sh` nor `q4-register-resolve.py` reads.** Both scripts fetch
`https://attendees.events.q4inc.com/rest/v1/event/<id>` for `status`/`msToStart`/
`broadcastUrl` and stop there. The same public, credential-free payload also has
`data.conference.conferenceCallIntake`:

```json
"conferenceCallIntake": {
  "meetingId": "411798645",
  "meetingUrl": "https://events.q4inc.com/analyst/411798645",
  "dialInDetails": {
    "US East Local": "+15855429983",  "US West Local": "+16268843620",
    "US Toll Free":  "+18334615787",  "CA Local":      "+13656574084",
    "UK Local":      "+441173890104", "UK Toll Free":  "+448081968935"
  },
  "password": { "encrypted": "031f6ddadfd3b9bc", "iv": "…", "tag": "…" },
  "type": "internal", "vendor": "chime", "q4Hosted": false
}
```

Fetched 2026-08-11 for CAVA (`411798645`) and CoreWeave (`683023279`).

### The three facts that make this a lane

1. **The numbers are UNIVERSAL, not per-issuer.** CAVA and CRWV return the
   *identical* six numbers, and all six appear on Q4's own public help page
   (`help.events.q4inc.com/eahc/international-dial-in-numbers`, 42 countries).
   It is one shared AWS Chime bridge behind every Q4-native webcast.
2. **The Meeting ID is the attendee-URL number — already public.** PROVEN by
   dial, not inferred: sending `411798645#` as DTMF to `+18334615787` produced
   an *event-specific* answer naming the company and its start time.
3. **The passcode plaintext is NOT in the public payload** — only AES-GCM
   ciphertext (8-byte ct ⇒ 8-char code). Q4's docs say it arrives in the
   registration confirmation email.

   ⛔ **RESOLVED 2026-08-11 DURING THE LIVE CALL, AND THE ANSWER IS "YES, YOU
   NEED IT".** This section first read "whether a listen-only caller needs it at
   all is UNRESOLVED: neither probe was ever asked for one." That inference was
   WRONG and it cost the whole phone lane on 26AUG11. Both pre-window probes
   stopped at "the event has not yet started", which is BEFORE the credential
   prompt — absence of a passcode challenge in a pre-window recording says
   nothing about the live call. Probed again at 21:11Z with the meeting ID,
   mid-call (CallSid in `corpus/bridge-probes/crwv_live_2026_08_11.wav`):

   ```
   Welcome to CoreWeave's 2nd Quarter 2026 earnings call. If you know your
   unique passcode, please enter it now. Otherwise, to speak to the next
   available operator, stay on the line.
   … Please hold while you are transferred to an operator for assistance.
   ```

   The Meeting ID alone reaches the issuer's own welcome — which is why it looks
   like it worked — and then drops you in the OPERATOR QUEUE, where a silent
   listen-only leg is hung up every ~75-90 s (Q-ADMIT Class B). Measured 26AUG11:
   CAVA up 21:01:52, dropped 21:03:06 (74 s); CRWV 4454 frames then 8273 then
   13272. **CRWV logged 0 detections across the whole run** — we were recording a
   hold announcement, not the call. CAVA's 2 "detections" were `a i` (k≤2)
   false-firing on that announcement, which is precisely what the short-token
   override exists to stop paying for.

   **The registration confirmation email does NOT carry the passcode.** Both
   26AUG11 confirmations listed Webcast Link + Email + "Meeting ID: 411 798 645"
   / "683 023 279" and nothing else. So the passcode is issued by some other
   path (analyst/participant registration at `events.q4inc.com/analyst/<id>`, or
   IR direct) and MUST be obtained before the call — there is no in-band way to
   get it once the call is running, and the operator queue is not a substitute
   unless a human is on the leg to answer the screener.

### Evidence — two live probes, 2026-08-11

**No DTMF** — CallSid `CAebb8523cc0c450788d1cda0f31a49818`, held 105.3 s, 5274
frames (`corpus/bridge-probes/q4chime_2026_08_11.wav`):

```
Please enter your meeting ID. The meeting ID you entered is invalid. …(×5)
A meeting ID is required to continue. … This call will be disconnected shortly.
```

**DTMF `411798645#`** — CallSid `CA5776f791ec8991cc46b490c9804ed67a`, held 21.1 s
(`corpus/bridge-probes/q4chime_cava_mid_2026_08_11.wav`):

```
CAVA Q2 2026 earnings call has not yet started. This event is scheduled to
start at Tuesday, August 11th at 5 p.m. EDT. Please reconnect up to 30
minutes before 5 p.m. EDT.
```

The second transcript is the whole finding: a public URL number, dialled into a
public phone number, returned the issuer's own event and its start time.

### Operational consequences

- **T-30 is a hard floor.** The bridge refuses before it and says so. The
  webcast, by contrast, opens at T-15 ("access the webcast up to 15 minutes
  before"), so **the phone lane is reachable 15 minutes EARLIER than the
  webcast** — on top of the 4.1–8.2 s in-call lead measured on CMG 26JUL29.
- **`admission` is still UNSET for this venue class.** Neither probe reached a
  conference, so the MSFT RULE is not satisfied and nothing was promoted into
  `configs/dial-ins.toml`. Re-probe at ≥ T-30 to classify held vs
  operator_screened vs passcode-walled.
- **Do not read `q4Hosted: false` as "no dial-in".** It was false on both events
  that nonetheless carry a full `dialInDetails` block; `dialIn.operatorLine` and
  `question.qaLine` were both `null` too. The conference block is the field that
  matters.

**Re-verify:** if Q4 rotates the shared bridge numbers, moves off Chime, or
starts requiring the passcode before announcing event status — the first two are
visible in the payload, the third only by dial.

## Q-26AUG12-MISSED — Air Canada and Nebius were same-time webcast regressions, not future events (2026-08-12)

**Time authority.** Air Canada's official advisory says its Q2 call was
Wednesday 2026-08-12 at 08:00 ET and identifies the public/media listen-only
webcast (`edge.media-server.com/mmc/p/5vxkq96v`). Nebius's official newsroom
and Investor Hub say its Q2 call was the same day at 08:00 ET and link a
registration endpoint on the event's OpenExchange subdomain. With the timezone
rules in force that is `2026-08-12T12:00:00Z` and 06:00 in
`America/Mexico_City`. Rechecked at `2026-08-12T18:44Z`: both calls were already
over; neither was “tomorrow.”

- Air Canada source:
  <https://www.aircanada.com/media/media-advisory-air-canada-to-present-second-quarter-2026-results/>
- Nebius announcement:
  <https://nebius.com/newsroom/nebius-group-announces-date-of-second-quarter-2026-results-and-conference-call>
- Nebius Investor Hub registration authority:
  <https://nebius.com/investor-hub>

**Access classification.** These are both webcast lanes, but an issuer link is
not itself acquisition proof. Air Canada explicitly labels public access
listen-only; its MediaServer attendee endpoint presented identity/firm fields
before player access in the event-day inspection. Nebius explicitly directs
listeners through registration; its OpenExchange registration/sign-in session
must exist before capture. Under the canonical contract these are schema-2
`registered-session` AccessPlans (or `signed-in-session` if an already-signed-in
profile is the actual prerequisite), with only a non-secret session label,
attester, and expiry recorded. Registration/login is an explicit external act;
the system must never infer success, submit identity autonomously, or bank a
cookie/token in the repository.

**Repository regression state, preserved read-only.** AC's historical webcast
config still carried a TODO start placeholder, so timer installation refused;
its legacy preflight/study failed and there is no schema-3 RunPlan, launch,
audio, book, order, or report evidence. NBIS's config held the correct
`2026-08-12T12:00:00Z` time but its only attempt is a schema-1 RunPlan with no
pinned event env; the schema-3 status boundary rejects it and the canonical
access date gate reports zero eligible RunPlans. `opsctl status 2026-08-12`
returns nonzero. No late RunPlan, registration, schedule, capture, or lifecycle
claim may be manufactured after the missed calls.

**Regression requirement.** A future event with either access class is ready
only if the same UTC-date pipeline produces: schema-3 RunPlan; explicit
schema-2 AccessPlan; measured readiness without changing pinned config bytes;
proof-bearing preflight; loaded scheduler readback; fire-time PCM and capture
proof; mandatory audio/book/order/FIX evidence; terminal audit; and explicit
alert acknowledgement. Missing any join keeps the date red while siblings
continue.

## Q-KALSHI-EVENT-CANDLES — public bounded activity history (2026-08-12)

**Verified contract.** Kalshi's public event-candlestick endpoint is
`GET /trade-api/v2/series/{series_ticker}/events/{ticker}/candlesticks`. It
requires integer Unix `start_ts` and `end_ts` bounds plus a
`period_interval` of 1, 60, or 1440 minutes. The response aligns
`market_tickers` with one candlestick array per market; each interval carries
fixed-point `volume_fp`. This is sufficient for the missed-arm grader's
bounded question: whether any market recorded positive volume at any point in
the UTC event-day window. A one-hour interval keeps a normal earnings event
comfortably below the endpoint's 5,000-candidate adjustment boundary.

**Live endpoint check, 2026-08-12.** Querying
`KXEARNINGSMENTIONETOR-26AUG11` over
`[2026-08-11T00:00:00Z, 2026-08-12T00:00:00Z)` returned all 15 markets active
and 26,419.37 contracts inside that bounded day. The postmortem's 26,496 is
the event's lifetime volume; the M5 grading predicate is deliberately the
weaker and correctly bounded fact `window volume > 0`, not equality with the
lifetime counter.

- Event candlesticks:
  <https://docs.kalshi.com/api-reference/events/get-event-candlesticks>
- Market candlestick field/interval semantics:
  <https://docs.kalshi.com/api-reference/market/get-market-candlesticks>
- Historical cutoff rules:
  <https://docs.kalshi.com/getting_started/historical_data>

**Boundary.** Markets settled before Kalshi's moving historical cutoff no
longer appear through live market/candlestick endpoints and require historical
market endpoints. `missed-arm-check.py` therefore treats any missing,
malformed, truncated, or transport-failed event history as `UNDETERMINED`; it
must never infer zero activity. Re-verify if Kalshi changes the event endpoint,
candlestick volume field, valid intervals, aggregation limit, or cutoff split.
