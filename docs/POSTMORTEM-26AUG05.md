# Post-mortem — 2026-08-05 (10 events, first real fills)

**Verdict: 8 COVERED / 2 DESCOPED / 0 MISSED.** The first day this system both
captured a full slate and put money on the wire. Also the day it proved it
cannot be trusted to describe its own behaviour — every headline number
reported during the day was wrong in the same direction.

Against 2026-08-04: **3 captured, 2 MISSED-ARM (SPOT, PGR), 0 orders.**
Today: **8 captured, 0 missed, 3 orders filled.**

## The slate

| event | bucket | lane | frames | drop% | det | intents | sent | spend |
|---|---|---|---|---|---|---|---|---|
| UBER | COVERED | webcast | 364,803 | 0.0 | 103 | 11 | 0 | — |
| DIS | COVERED | webcast | 436,996 | 0.0 | 122 | 12 | 0 | — |
| SHOP | COVERED | webcast | 560,778 | 0.0 | 26 | 5 | 0 | — |
| YOU | COVERED\* | webcast | — | — | 24 | 2 | 0 | — |
| DASH | COVERED | webcast | 168,800 | 3.3 | 21 | 7 | **1** | 97¢ |
| TLN | COVERED | **phone** | 227,209 | 4.1 | 4 | 1 | 0 | — |
| MELI | COVERED\* | webcast | — | — | 79 | 4 | **1** | 97¢ |
| FIG | COVERED | webcast | 285,471 | **11.3** | 99 | 5 | **1** | 97¢ |
| DUOL | DESCOPED | — | — | — | — | — | — | — |
| EBAY | DESCOPED | — | — | — | — | — | — | — |

\* captured and detected but wrote **no report.json** — see Defect 1.

**Rate: 8/10 captured audio, 8/10 detected, 3/10 placed orders.** Both descopes
carry a `DESCOPED.json` record, so nothing falls to MISSED under the MCD rule.

Detection quality across settled events: **42 words said, 35 detected, 43 fired
— recall 83%, precision 81%.**

## The fills

| market | filled | fee | settled | net |
|---|---|---|---|---|
| DASH-AFFO ("afford") | 95¢ | 0.34¢ | YES | +4.66¢ |
| FIG-CODE ("code") | 57¢ | 1.72¢ | YES | +41.28¢ |
| MELI-AI | 90¢ | 0.63¢ | pending | +9.37¢ if YES |

≈ **+55¢ on $2.42 deployed.** Three for three — but the risk/reward is not what
the result suggests. Buying at price *p* wins `100−p` and loses `p`, so it needs
precision > *p*. Against measured 81%: the 95¢ and 90¢ fills were **negative-EV
bets that won**; only CODE at 57¢ was structurally sound. `ev_gate` cannot catch
this — it computes `(100−cap−haircut)·count − fee`, which assumes the detection
is *correct*, and has no term for P(wrong). Harmless at a 1¢ cap; not harmless
at 97¢.

> **AMENDED 2026-08-07 — the paragraph above is wrong, and its error is the one
> it was warning about.** It applies a *pooled* precision to a bet whose price
> already encodes the answer. `scripts/settle-score.py` joins real settlement to
> every banked run record (n=64 first-intents) and conditions on the observed
> ask:
>
> | ask band | n | YES | P | 95% CI | EV@mid | EV@CIlow |
> |---|---|---|---|---|---|---|
> | 0–19¢ | 3 | 1 | 0.33 | [0.06, 0.79] | +22.3¢ | −4.9¢ |
> | 20–39¢ | 11 | 2 | **0.18** | [0.05, 0.48] | −13.8¢ | −26.9¢ |
> | 40–59¢ | 8 | 3 | 0.38 | [0.14, 0.69] | −14.5¢ | −38.3¢ |
> | 60–79¢ | 6 | 5 | 0.83 | [0.44, 0.97] | +11.3¢ | −28.4¢ |
> | 80–94¢ | 7 | 4 | 0.57 | [0.25, 0.84] | −30.9¢ | −63.0¢ |
> | 95–100¢ | 29 | 29 | **1.00** | [0.88, 1.00] | +1.0¢ | −10.7¢ |
>
> **Precision is a function of price, not a constant.** Where the market is at
> 95–100¢ this system has been right 29 times out of 29; where it is at 20–59¢,
> 5 of 19. AFFO at 95¢ and MELI-AI at 90¢ were not negative-EV bets that won.
> The genuinely lucky fill was CODE at **57¢** — that band settles YES 38% of
> the time. And the cheap asks this postmortem implies we should have preferred
> were losers: DASH-ROBO at 37¢ and CELH-ESSE at 39¢ both settled **NO**.
>
> What survives is the *conclusion*, not the reasoning. **Do not raise the
> notional envelope** — but because nothing is proven, not because 97¢ is
> negative. Every `EV@CIlow` in that table is negative. Break-even at 97¢ needs
> p ≥ 0.98, and 29/29 with an interval floor of 0.88 cannot separate 0.98 from
> 0.90, where one NO costs 97¢ and erases ~48 winners. Sixty-four scored
> decisions is not a sample you can tune a money policy on.
>
> What survives intact is that `ev_gate` has no P(wrong) term. The fix is *not*
> a per-keyword precision prior — replayed against these events that policy
> skips the winning 97¢ trades and buys the losing cheap ones, turning +25.0¢
> into −4.0¢. Any correctness term has to condition on price. Until there are
> enough samples to fit one, EV is recorded in shadow and gates nothing.
>
> The lead worth chasing: 2 of the 11 intents at 20–39¢ **did** settle YES —
> real early calls the market had not priced. Alpha is in separating those two
> from the other nine, which is a targeted precision question about which words
> false-fire against a disagreeing market, not an argument for a new model.

## What the enforced prep chain caught

The stage→study→preflight→green-gate chain earned its keep:

- **Dead words**, before they could be mistaken for detector success: DASH
  `TAIL`, DIS `FORT`, TLN `SHAP` — all scored 0 confirmed in the nightly study
  and were acked, so their silence was known-unserved rather than assumed-heard.
- **The digit-strip class.** `market-phrase-screen.py` refused DUOL's
  `-100` market: it resolves on "100 Million" but `prepare` had armed bare
  `million`, which fires without resolving the market — a false positive by
  construction. That is the AAPL "iPhone 18" → "iphone" failure, caught this
  time before the call rather than after.
- **Preflight recovery**: DIS went 4 fails → 0, DASH 1 → 0. Both would have
  armed red without it.
- **The green gate refused a config whose sha had changed**, forcing a
  re-preflight after the caps were raised. Exactly its job.
- **A stale launchd job.** The plists were edited but not reloaded; `launchctl
  print` still carried the pre-edit arguments. Had that shipped, all six
  afternoon events would have refused to arm (`mention_probe_gate`: cap 97¢ >
  1¢). Caught by inspecting the *loaded* job, not the file.

## What it missed

Five defects, all sharing one property — **the system could not describe what it
had done, and nothing objected.**

1. **A run that dies leaves no record.** `arm()` wrote `report.json` on exactly
   one of ~14 exit paths. MELI armed, took 79 detections, sent a real order,
   then hit the pipe idle guard at 22:01:50Z — and left nothing. On disk that is
   indistinguishable from an event that never armed. YOU died the same way at
   12:36:15Z. *Fixed today (Section 1).*
2. **The report called intents "orders".** `orders.len()` counts every non-fire
   early return, and the `[LIVE]` tag came from `!dry_run`. The morning was
   reported as **"23 orders placed" when the true number was 0** — every record
   had short-circuited at the non-crossing branch before `send_order`. Both ops
   graders inherited the same count. *Fixed today (Section 2).*
3. **The book-staleness gate checks the wrong clock.** `executor.rs` passes
   `cache.feed_age_ns()` — a socket-wide liveness clock held under ~500 ms by
   the ping loop — into the `check_order` parameter *named* `book_age_ns`. The
   real per-ticker age is computed, written to the report, and never gated on.
   Consequence: **all three fills were made against stale books** — AFFO at
   **40,597 ms** against a 2,000 ms limit, CODE at 2,657 ms. DASH-UBER at
   96,774 ms was refused for *EventNotional*, checked twelve lines after
   `BookStale`, which proves `BookStale` never fired. Worse second-order effect:
   the single order the event envelope can afford went to whichever detection
   arrived first, not the freshest — AFFO burned it on a 40-second-old snapshot
   while LOCA and ROBO, with 6–7 ms books, were refused. *Open (Section 3).*
4. **The box was never used.** `us-east-1` sat at load 0.00 all day with Caddy
   and the TLS ingress running. Everything ran on the CDMX laptop over ngrok,
   because the box's binary predated the `MENTION_FILL_CAP_CENTS 50→97` change
   and `mention_fill_gate` would have refused a 97¢ cap. **The blocker was one
   `cargo build` — discovered at T−60, when migrating was no longer safe.** Cost,
   from the pinned baselines: `media.twiliocdn.com` p50 **55.3 ms vs 2.9 ms** and
   **4.0% packet loss vs 0%**; Kalshi p50 54.7 ms vs ~2–3 ms.
5. **`daily_loss_cap_cents` does not exist at runtime** — parsed, defaulted,
   clamped, never read. There is no P&L accumulator. The $1,000 daily-loss
   backstop is a comment. *Open (Section 4).*

## Host contention

The laptop carried five arms plus four other agent sessions, a ChatGPT/Codex
agent, a postgres analytics query and Terminal. **Load peaked at 150 on 8
cores.** The arms needed ~0.5 cores between them; the events were victims, not
cause. It showed up as lost audio: FIG **11.3%** of frames, TLN 4.1%, DASH 3.3%,
and FIG's capture→decision p95 hit **42 seconds**. Three words that were
demonstrably said were never heard (TLN `FOSS`, DASH `WAYM`, FIG `CODI`).

## The phone lane

TLN was the only phone event and the only one that produced a measurable race.
Against Kalshi's public trade tape, `BEHI` sat at 53¢ untouched for 35 minutes,
then swept 53¢→99¢ in 404 ms. **We decided 534 ms after the first print**
(≈640 ms if the host's measured −0.101 s clock offset held). We lost — but the
morning's webcast races had a **median same-utterance deficit of 3.23 s**, so
the phone lane compressed the gap roughly **6×**. Losing that one cost nothing:
at a 99¢ ask the EV gate correctly refuses, since 1¢ gross minus 1¢ fee leaves
zero against a 2¢ minimum.

The in-window admission probe held **119.0 s of a 120 s cap**, proving the
reservation-window venue class is armable unattended. Auto-promote silently
failed on a `tomllib` ImportError under launchd's PATH, so the arm proceeded
under a paged green-gate override — the correct fallback for a phone-only event
with no webcast to fall back to.

## Ranked fixes for 2026-08-06 (AAOI, ABNB, CELH, LYFT, NBIS staged)

1. **Run on the box.** Single biggest lever: removes 3–11% frame loss, the
   42-second tails, the ngrok hop and the CDMX RTT in one move, and takes the
   capture host away from unrelated desktop load. Requires only that the binary
   be rebuilt and pinned **the night before** — that is the whole of what
   blocked it today. The `phone-event-prep` skill covers phone events; webcast
   lanes still need the same treatment.
2. **Finish the measurement/money sections.** 3 (staleness gate honest, book-age
   policy defaulting off), 4 (dead `daily_loss_cap_cents`, unpersisted
   `event_spent_cents`, no credit-back on proven-absent sends), 5 (EV/precision
   shadow instrumentation, no gating).
3. **Protect the capture host.** Nothing else heavy runs on it during a slate.
   Today's shed rule assumed EARSHOT was the load; it was 37% of it.
4. **Fix the registry claims that lied.** MELI's `webcast_note` said "no
   registration"; it returned `loginType 1, pageId: register` and the resolver
   exited empty at T−18. Re-verify `webcast_note` the same way dial-ins are
   re-verified.
5. **OpenExchange/KnowledgeVision is now solved on paper** — the harvest recipe
   and both player ids are pinned in `configs/dial-ins.toml`. DUOL and EBAY were
   descoped for want of that recipe plus host load; the class is workable at
   T−5 next time.

Do **not** raise the notional envelope yet. At 90–95¢ fills with 81% precision,
more orders per event mostly buys more negative-EV fills. Price discipline
first, then size.

> **AMENDED 2026-08-07.** The ruling stands; the reason does not. Conditioned on
> price, 90–95¢ fills are not negative-EV — see the amendment under "The fills".
> Hold the envelope because **no price band is proven** at n=64, not because the
> expensive ones are bad. And read "price discipline" carefully: the measured
> loser is the *cheap* end (20–59¢ settles YES 5 times in 19), so discipline
> here means declining low-priced disagreement with the market, which is the
> opposite of what this line reads like at a glance.
