# casino143 — EARSHOT: what it is, how it connects, and why it keeps breaking

> Historical diagnosis frozen at 2026-08-11. Several remedies below are now
> implemented; `docs/STATUS.md` and `docs/runbooks/EVENT-LIFECYCLE.md` are the
> current authorities.

**Audience:** an engineering agent with broad tooling who will simplify this
system. **Author:** the agent that ran the 2026-08-11 CAVA + CoreWeave events.
**Status of this file:** historical description and diagnosis only. Written
2026-08-11, immediately after that event pair; it does not track later fixes.

**Your goal, stated by the operator:** before chasing a latency-winning system,
make it *reliable*. Every event should get a steady audio connection, a steady
Kalshi book connection, a recorded tape to measure speed and accuracy against,
and one clear procedure that runs the same way every time. Simplify the
architecture so the recurring dumb failures stop.

**The most important thing to understand before you change anything:** the
*design* of this system is good. `docs/ARCHITECTURE.md` describes a clean,
typed, three-pipeline machine with 47 registered nodes and enforced ownership.
The problem is almost entirely in the **operational layer that grew around it**
— 148 shell/python scripts, 165 configs, and a runbook that is a chronological
diary rather than a procedure. Do not rewrite the Rust core looking for the
bug. The bug is that a correct core is driven by an improvised harness.

---

## 0. The 60-second summary

EARSHOT trades Kalshi "mention markets": binary contracts on *will company X say
word Y during its earnings call*. The system listens to the call live, detects
keywords in the audio, and buys YES on the corresponding market faster than the
market reprices.

Money is made in the seconds between a word being spoken and the market
noticing. That means three things must all work at once:

1. **Audio** — a live, unbroken feed of the call (phone bridge or webcast HLS).
2. **Book** — a live Kalshi WebSocket order book, so the executor knows what is
   offered and at what price.
3. **Detection → decision → order** — under a latency budget, with risk bounds.

On 2026-08-11 all three were individually working, and the night still went
badly, for reasons that are entirely about plumbing and procedure. That is the
pattern.

---

## PART I — The architecture as designed

### I.1 Three pipelines

From `docs/ARCHITECTURE.md`. The unit is a **node**: one job, one owner, one
input contract, one output contract, one verification path. The machine-readable
map is `architecture/nodes.toml` (47 nodes); `docs/NODES.md` is the human index.

```
CONTROL   (getting ready for an event)
discover → qualify ─┬→ prepare(EventConfig) ────┐
                    ├→ admission(ArmPermit) ────┼→ preflight → launch/bind
                    └→ acquire(SourceEndpoint) ─┘                │
                                       listener_ready            ▼
                                              [phone session] → phone_attached
                                                                     │
                                                    supervise + telemetry → grade

RUNTIME   (the live hot path)
source → preproc → chunker → detector.port → decision → executor → PipelineReport
                                 ▲
             keyword compiler → engine constructor
                                              ▲        ▲
                                           book     risk/auth

EVIDENCE  (learning from what happened)
capture → curate corpus → benchmark/measure → reviewed verification pin
```

Read that third pipeline again. **EVIDENCE is a first-class pipeline in the
design, and in practice it is the one that silently doesn't run.** See §III.5.

### I.2 The runtime data plane

```
config.toml ──► config ──────────────────────────────┐ (arm-time wiring)
                                                     ▼
[source] ─ring─► [preproc] ─ring─► [chunker] ─ring─► [detector]+[decision]
 tokio            OS thread         OS thread          OS thread "detect"
                                                     │
                                          mpsc(64)   │ DecisionIntent
                                                     ▼
             [book-cache] ◄─WS── Kalshi        [executor] ──HTTPS──► Kalshi
              tokio task    (reads: ArcSwap)    own OS thread
                                                     │ guarded by
                                                 [risk gate]
```

Key properties worth preserving through any simplification:

- **Rings between stages**, so a slow stage drops frames rather than blocking.
- **Book reads are wait-free** (`ArcSwap`); the executor never does a blocking
  REST fetch on the hot path (`src/kalshi/book.rs:1-8`).
- **Money is integer cents, quantities integer centi-contracts.** Kalshi supports
  fractional contracts; rounding them cost a real best-ask on 2026-07-15
  (`src/kalshi/book.rs:17-22`).
- **Fanout variant** (`[arbiter] mode = "fanout"`): N sources each get a full
  independent chain, OR-ed at a one-shot decision table. Different legs hear
  different audio. This works and is proven.

### I.3 Source lanes

| lane | module | how audio arrives |
|---|---|---|
| **phone** | `src/source/twilio_ws.rs` | Twilio dials the bridge, media streams to our WSS listener |
| **webcast** | `src/source/pipe.rs` | `streamlink`/`ffmpeg` subprocess pulls an HLS m3u8 |
| **replay** | `src/source/replay.rs` | offline corpus, for tests and benchmarks |
| **sip** | `src/source/sip.rs` | alternative telephony ingress (bake-off) |

Phone is measured **4.1–8.2 s faster** than webcast on the same call (CMG,
2026-07-29). Phone is therefore the preferred lane *when admission works* — and
admission is where it keeps failing (§III.2).

### I.4 The two composers

`CLAUDE.md` is explicit: current wiring lives in `pipeline::run_pipeline`
(audio chain) and `main.rs::arm` (execution side). **Do not add a third
composer.** The roadmap migrates both behind an explicit run supervisor. If your
simplification introduces a new orchestration path, it must *replace* one of
these, not sit beside it.

### I.5 The CLI

`src/main.rs` exposes: `GenFixture, Replay, Arm, ProbeOrder, KillDrill, Prepare,
Book, Cancel, Orders, BenchDetector, CompileKeywords, DetectorSmoke,
DetectExplain, Latprobe, CaptureBakeoff, BridgeProbe`.

`Arm` is the only one that can place an order. `CaptureBakeoff` and `BridgeProbe`
have no detector, no risk gate, and no Kalshi connection — they cannot influence
an order, which is why they are safe to run alongside a live arm.

### I.6 Risk model (`src/risk.rs`)

Hard ceilings are code constants; config may only tighten:

```
HARD_MAX_ORDER_NOTIONAL_CENTS = 50_000   ($500)
HARD_MAX_EVENT_NOTIONAL_CENTS = 200_000  ($2,000)
HARD_DAILY_LOSS_CENTS         = 100_000  ($1,000)
MENTION_FILL_CAP_CENTS        = 97       per-ORDER price bound
PROBE_LEDGER_CAP_CENTS        = 13_000   ($130) — see §IV.7, it is PER-EVENT
```

Order postures, in increasing danger:
- **probe** (`--allow-mention`): cap ≤1¢, non-crossing, *provably cannot fill*.
- **fill** (`--fillable`, requires `--i-am-live --allow-mention`): may cross the
  ask up to `MENTION_FILL_CAP_CENTS`.

The kill switch cancels **only** `OrderRegistry` ids — this Kalshi account
carries unrelated resting orders from other systems. Never widen that scope.

---

## PART II — The event lifecycle as actually practiced

This is the procedure the operator wants formalised. Today it exists as a chain
of scripts that a human (or an agent) invokes, with the ordering held in
`docs/RUNBOOK.md` and in people's heads.

### II.1 The chain

| # | stage | script | produces |
|---|---|---|---|
| 1 | **discover** | `discover-events.py`, `slate-daily.sh` | slate of upcoming mention events |
| 2 | **hunt dial-in** | `hunt-dial-in.py`, `mine-dial-in.py` | phone numbers → `state/hunt/numbers.env` |
| 3 | **qualify venue** | `dial-in-intake.py`, `registry-propose.py` | entry in `configs/dial-ins.toml` |
| 4 | **stage** | `slate-stage.sh`, `stage-webcast-arm.sh` | `configs/<event>-arm.toml` from a template |
| 5 | **prepare keywords** | `earshot prepare` | keyword blocks + allowlist from the live board |
| 6 | **study** | `variant-study.sh`, `study-nightly.sh` | `configs/.readiness/<base>.json` (RECALL, TTS) |
| 7 | **posture** | `posture-from-study.py` | per-word FILL/OBSERVE caps + sizes |
| 8 | **admission probe** | `bridge-probe.sh` | `admission=` verdict in the registry |
| 9 | **preflight** | `preflight-event.sh` | `state/<E>/preflight-result.json` + **pinned binary** |
| 10 | **green gate** | `assert-ready.sh` | PASS/REFUSED — launchers call this |
| 11 | **host check** | `box-ready.sh` | GO/NO-GO for the us-east-1 box |
| 12 | **schedule** | `box-event-install.sh`, `install-event-timer.sh` | systemd user timer |
| 13 | **arm** | `event-autorun.sh` → `launch-event.sh` (phone) / `launch-webcast.sh` (webcast) | live run |
| 14 | **supervise** | `redial-supervisor.sh`, `green-guardian.sh`, `watcher-liveness.sh` | keeps it alive |
| 15 | **pull** | `pull-box-state.sh` | box artifacts → laptop |
| 16 | **grade** | `missed-arm-check.py`, `settle-score.py` | verdict per issuer |
| 17 | **score precision** | `score-precision.py` (new 2026-08-11) | true/false per detection |

### II.2 The gates, and what they actually enforce

- **`assert-ready.sh`** — the green gate. Requires (a) a preflight verdict that
  `pass==true`, whose `config_sha256` matches the *current* config bytes and is
  ≤24 h old; (b) `configs/.readiness/<base>.json` whose `keywords_sha256`
  matches, with no keyword at `confirmed==0` unless acked.
- **`keywords-extract.py --sha256`** hashes **tickers, phrases, variants only** —
  *not* price caps. So a posture change does **not** invalidate a study, but it
  *does* invalidate preflight (config bytes changed). This distinction is
  load-bearing and non-obvious.
- **`preflight-event.sh`** pins a release binary into `state/<E>/earshot-pinned`.
  Fire-time compiles are impossible under the scheduler's minimal PATH by
  design — the pin is the only unattended-safe path.

### II.3 Hosts

- **Laptop** (CDMX, 8 cores): browser work, staging, studies. Contended — it ran
  at load 25–100 on 2026-08-11 with ChatGPT, a VM, and other agents' builds.
- **Box** (`ubuntu@3.212.174.94`, us-east-1, 16 cores): the correct home for live
  runs. Idle at load 0.00. Has TLS ingress (`media.3-212-174-94.sslip.io`,
  routed paths `/e/8080`…`/e/8085`, `/e/8099`), systemd user timers, lingering
  enabled, credentials at `~/.earshot/*.env` and `~/.kalshi/`.
- **Critical asymmetry:** `deploy-box.sh` **excludes `state/` in both
  directions**. A box-hosted run leaves *nothing* on the laptop until
  `pull-box-state.sh` runs. Every grader reads a box run as zero unless you pull
  first. The box also has **no playwright**, so browser-based stream resolution
  must happen laptop-side and be pushed.

---

## PART III — The five failure classes the operator named

Each is stated in the operator's words, then what the evidence actually shows.

### III.1 "Bad infra regarding actual fills (no resolver for liquidity)"

**What happened 2026-08-11:** 11 decision intents across both events. Outcomes:

| word | tranche | `best_ask` at decision | outcome |
|---|---|---|---|
| CAVA SALM | 10 | `None` | LOST_RACE — empty book |
| CAVA EXPA | 10 | `None` (book 24.7 s old) | LOST_RACE |
| CAVA SHRI | 10 | `None` (book 161.5 s old) | LOST_RACE |
| CAVA CYCL | 1 | `None` (book 244.7 s old) | LOST_RACE |
| CRWV STOR | 10 | **`(98, 5400)`** = 54 contracts @ 98¢ | LOST_RACE — **cap is 97** |
| CAVA CATE/SAME/ACCE/DINI | 10 | crossed | **filled, $9.70 each** |
| CAVA/CRWV AI | 1 | crossed | filled, $0.97 ×4 |

Two distinct problems are being conflated under "liquidity":

1. **Most books are genuinely empty.** Verified independently via the orderbook
   endpoint post-call: `yes=None, no=None`. There is nothing to buy on most
   mention strikes most of the time. This is a *market structure* fact, not a
   bug.
2. **When depth exists, it sits above the 97¢ ceiling.** Two independent
   observations: HIMS `collaboration` at **99¢ × 523 contracts** (2026-08-10),
   CRWV `storage` at **98¢ × 54 contracts** (2026-08-11). Both excluded by
   `MENTION_FILL_CAP_CENTS = 97`.

**What is missing** is not a "liquidity resolver" in the sense of finding
liquidity — it is:
- No **pre-arm liquidity survey**: nobody checks, at stage time, whether an
  event's strikes have ever had resting size. We arm 17 markets and discover at
  T+0 that 15 are empty.
- No **depth-aware sizing**: tranche 10 is applied uniformly whether the book
  shows 54 contracts or nothing.
- No **book tape** for the armed event (§III.5), so "was there really nothing, or
  was our feed stale?" cannot be answered after the fact. On 2026-08-11 book ages
  at decision ran **1.9 s → 7.4 s → 31 s → 161 s → 245 s**. Ages that large are
  *consistent* with genuinely quiet markets, but nothing proves it.

**The 97¢ ceiling deserves a deliberate decision.** At 97¢ a word must be right
>97% of the time to break even (10 contracts: +30¢ if right, −970¢ if wrong).
The only large historical win, FIG `CODE`, filled at **57¢**, where break-even is
57%. Cheap fills are where the edge is; the ceiling is currently set exactly
where `ev_gate` cuts out, which is not the same as where EV is best.

### III.2 "Bad infra regarding actual phone connections (constantly being dropped)"

**Root cause, established live on 2026-08-11 by dialling the bridge mid-call and
transcribing it:**

> *"Welcome to CoreWeave's 2nd Quarter 2026 earnings call. **If you know your
> unique passcode, please enter it now.** Otherwise, to speak to the next
> available operator, stay on the line."* → *"Please hold while you are
> transferred to an operator for assistance."*

The Meeting ID gets you to the issuer's welcome — which **looks like success** —
and then parks you in the operator queue, where a silent listen-only leg is hung
up every **~75–90 s**. Measured: CAVA up 21:01:52, dropped 21:03:06 (74 s).
CRWV logged **0 detections across 4,454 frames** — it was recording a hold
announcement, not a call.

This is Q-ADMIT **Class B** (`VERIFICATIONS.md`), already documented from MSFT in
July: operator-screened bridges drop silent callers in 42–73 s.

Compounding infrastructure problems:
- **`--max-redials` defaulted to 2.** Against a bridge that drops on a fixed
  ~90 s cadence, 2 redials means dead in ~3 minutes. Raising it to 40–60 live
  turned a fatal condition into ~2 s of loss per cycle.
- **Each redial restarts the arm**, which resets the one-shot decision table and
  overwrites `report.json` (§IV.3, §IV.4).
- **The passcode is not in the registration confirmation email.** Both 26AUG11
  confirmations listed Webcast Link, Email, and Meeting ID — no passcode. It must
  come from analyst/participant registration or IR directly, and it must be
  obtained *before* the call. There is no in-band recovery.

**What is missing:** a venue-class model that says *"Q4-native bridge ⇒ needs a
passcode ⇒ acquire it at stage time or plan for the operator path"*, and a
redial policy derived from the venue's observed drop cadence rather than a
default of 2.

### III.3 "Bad event management — the operator handoff is not automatic"

The operator's exact complaint: *"for operator calls I have to explicitly ask for
the handoff to my cell as the system doesn't do it automatically; so if I'm not
monitoring live the event gets missed altogether."*

**This is exactly right, and the mechanism is already built but unreachable.**

`docs/OPERATOR-HANDLER.md` describes a working human-first path
(`operator-page.sh` → `twilio-operator-dial.sh`): page the operator's cell into a
Twilio `<Conference>`, *then* dial the bridge into the same room with a
`<Start><Stream>` fork so the human's voice stays off the detector's track. It
was built and rehearsed 2026-07-30, 3/3 detections.

**Why it did not fire on 2026-08-11:** `event-autorun.sh` auto-escalates only
when **both** hold:
1. the venue's `configs/dial-ins.toml` entry says `admission = "operator_screened"`, **and**
2. the event config carries `[operator_handler] enabled = true`.

Condition (2) was true. Condition (1) was **not**, because we had never been
screened by this venue before — the registry cannot know a venue is
operator-screened until *after* a call where it screened us. **The gate depends
on knowledge that only the failure itself produces.** That is the design flaw.

Additional gaps found live:
- When paged manually, `operator-page.sh --wait-joined 45` rang once, was not
  answered, and correctly fail-closed. There is **no retry, no escalation, no
  second channel**. One unanswered ring = the lane is dead.
- Failure notification is **email only** (`notify.sh`). On 2026-08-11 a "max
  attempts reached" summary was emailed at 20:44 and nobody read it for 13
  minutes. There is no push, no SMS, no call for a *failed* event — the cell is
  only ever dialled for the operator-conference path.

**What is needed:** detect the ~75–90 s drop cadence *in-run*, classify it as
operator-screened without waiting for a human, page the cell on the **first**
drop, retry the page, and make a dead lane page rather than email.

### III.4 "Bad overall tidiness — we patch premarket instead of being ready"

Quantified, today:

| | count |
|---|---|
| scripts in `scripts/` | **148** |
| configs in `configs/` | **165 `.toml` + 24 `.env`** |
| event state dirs | 62 |
| top-level `.md` (incl. 3 stale HANDOFF files) | 8 |
| `docs/` markdown | 28 top-level + 36 node contracts + 13 layer contracts |
| Rust | 17,331 LOC across 48 files |

`docs/RUNBOOK.md` is **2,328 lines** and opens with
`# NEXT EVENT — Monday 2026-07-20, Domino's (DPZ) Q2 call` — three weeks stale.
It is a chronological diary of every event ever run, with per-event runsheets
interleaved with hardening notes and post-mortems. **There is no single
"how to run an event" procedure anywhere in the repo.** The lifecycle in §II.1
was reconstructed for this document by reading 148 script headers.

Consequences observed on 2026-08-11:
- The configs were staged the night before with `starts_at = midnight UTC`
  placeholders. Left alone, every T-75 job schedules **already in the past**
  (runs=0, silent total miss — this exact failure lost all 5 events on 08-06).
  It was caught only because an agent happened to check.
- Posture, caps, ledger constant, acks, registry entries, and the box binary were
  all edited **on event day**, some within 4 hours of the call.
- `cargo test` currently fails: `doc-lint` reports **32 errors**, mostly
  "dirty governed path has no open claim" — the claim system in
  `architecture/claims.toml` / `docs/work/ACTIVE.md` has drifted from practice.

### III.5 "Desmadre — we miss pipeline parts, like collecting tape"

**The single most damaging instance, 2026-08-11:** both webcast arms filled 5
orders totalling **$48.50** and banked **no audio at all**. `state/<E>/` contains
`journal.jsonl`, `launch.log`, `report.json` — and no `.wav`.

Therefore **it is impossible to establish whether "catering", "same store",
"acceleration", "dining", or "government" were actually said.** We bought
positions we cannot audit. The detector's own claim is the only evidence, and a
detector cannot be its own ground truth.

By contrast HIMS 2026-08-10 *was* auditable — scored **9/9 true positives**,
every hit within ±3.2 s — purely because `capture-sidecar.sh` happened to be
running that night. The difference between an auditable event and an unauditable
one was whether a human remembered to start a second script.

**Two tapes are missing, not one:**

1. **Audio tape.** `capture-sidecar.sh` exists and works. It was opt-in.
   (An always-on wiring was added to `launch-webcast.sh` on 2026-08-11; the phone
   lane still has no equivalent.)
2. **Book tape.** `examples/book-tape.rs` exists and writes a Kalshi WS tape —
   but it is wired only into `xvenue-autorun.sh`, the cross-venue *research*
   observatory. **No event arm records the book.** So when `best_ask=None`
   appears at decision time, there is no way to reconstruct whether the market
   was empty or our feed was stale. There is also a known unfixed defect:
   *"book-tape runs past its deadline and goes deaf"* — a `--secs 14400` capture
   lived 2d21h and stopped writing 26 h before that with no `close` row. Check
   tape **growth**, never process existence.

Without both tapes, **latency cannot be measured either.** `Δrace` (how far ahead
of the market we fired) requires a book tape to compare against. Every latency
claim in this repo comes from special-purpose research runs, not from events.

---

## PART IV — Catalogue of concrete recurring defects

These are specific, reproducible, and each has cost a real event. Fix or
consciously accept each one.

### IV.1 Host-absolute paths baked into files that run on another host
`phone-lane-derive.py` wrote `ACCOUNT_ENV="/Users/bernardodelrio/.earshot/ms.env"`
into an env file that only ever executes on the box. **Result: both events dead
20:35–20:44 on 2026-08-11**, 25 dial attempts each returning `rc=2` in zero
seconds, then "max attempts reached" and stop. The same file already solved this
for `EARSHOT_NUMBERS` with a search loop and its own comments warn about the
class. *(Fixed 2026-08-11 for `ACCOUNT_ENV`; audit for other instances.)*

### IV.2 `send_ts` null on orders that executed
`state/<E>/report.json` carried an order with `order_status: "executed"`,
`price 97`, `count 1`, but `send_ts = null` and top-level `sent = 0`. The
standing counting rule — *count `send_ts != null`, never `.orders | length`* —
therefore scores a **real fill as zero**. This is the inverse of the 2026-08-05
overcount the rule was written to fix. The webcast lane populates `send_ts`
correctly; the restarting phone lane does not.

### IV.3 `report.json` overwritten on every arm restart
Each `event-autorun.sh` cycle rewrites `state/<E>/report.json`. The phone lane
restarted every ~90 s; three orders were placed, the surviving report shows one.
**Only `probe_ledger.json` accumulated correctly** (291¢). Graders read the
report.

### IV.4 One-shot decision table resets on restart
Same root cause. Each restart lets an already-fired word fire and buy **again**.
This is how `a i` bought **four times off the same hold announcement**. Nothing
caps repeats across restarts except the event notional and the ledger.

### IV.5 `missed-arm-check.py` excused a real miss using a config that was never armed
It globs `configs/*.toml` by event-id prefix and counted
`configs/crwv-26aug11-raw.toml` — the raw `prepare` output, never armed, still at
1¢ caps — as a **lane**. Its `fill_posture=False` dragged the issuer verdict to
`ARMED-OBSERVING: "0 orders is the 1¢ non-crossing posture… not a miss"` on an
event that had actually spent **970¢** and whose board traded 28,215 contracts.
It converted *"we don't know yet"* into *"this was intentional"* — the worst
direction for a grader to be wrong in.

### IV.6 `book_stale_enforce` defaults false, and the ages are enormous
`src/risk.rs` configures `book_stale_ms = 2000` but only **warns** unless
`book_stale_enforce = true`. Every fill on 2026-08-11 crossed on a stale book:
`SAME` at 7.4 s, `GOVE` at 2.6 s, `ACCE` at 31.4 s. Decisions were taken on books
up to **245 s** old. The documented evidence for leaving enforcement off (a
2000 ms limit would refuse 43% of intents, including 2 of the 3 lifetime fills)
was gathered when an order was **1 contract at 97¢**. At **10 contracts** the
same stale quote risks $9.70. Note the subtlety: age is per-ticker, and large
ages correlate with genuinely quiet markets, so naive 2 s enforcement would
refuse most of the board. This needs a real number (perhaps 30 s), not a flip.

### IV.7 `PROBE_LEDGER_CAP_CENTS` is documented as cumulative but is per-event
`RiskGate::new` loads the ledger from `state_dir.join("probe_ledger.json")`
(`src/risk.rs:246`), so each event starts a fresh file at zero. The doc comment
above the constant claims *"cumulative real-money spend across ALL live
drills/sessions… persisted across runs."* Real exposure is **$130 × number of
events**, not $130. The constant went 100 → 2000 → 7000 → 13000 in two days as
sizing changed; each raise was necessary and each made the mischaracterisation
more expensive.

### IV.8 `admission-window-probe.sh` targets the wrong window for this venue class
It probes T-90…T-30. The Q4/Chime bridge **opens at T-30** and refuses before it
("please reconnect up to 30 minutes before"). The tool sends the operator to
probe at precisely the useless time.

### IV.9 Stale comments that mislead in the expensive direction
`launch-webcast.sh` claimed *"`--fillable` is still refused here"* while its own
parser had accepted it since 2026-08-05. Read literally, that says the webcast
lane cannot fill — an operator pricing a FILL posture would conclude the config
is inert and hunt a nonexistent bug. Same file quoted a "50¢ cap" and a "$1
ledger" long after both had changed. *(Corrected 2026-08-11.)*

### IV.10 Point-in-time checks treated as durable
`board-open.py --issuer ETOR` returned *"no open board"* at ~15:00Z, so ETOR was
deprioritised and never re-checked. Its board later traded **26,496 contracts
across 15 markets** and we placed nothing — graded `MISSED-ARM`. Board existence
must be re-checked at arm time, not once during staging.

### IV.11 Posture-flattening in derivation
`phone-lane-derive.py` rewrites **every** keyword to one uniform cap and budget.
Left unchecked on 2026-08-11 it would have given `a i` — the k≤2 short token the
system deliberately denies money — a **$9.70** budget. Per-word posture had to be
re-applied after derivation. Any tool that regenerates a config must preserve, or
explicitly recompute, posture.

### IV.12 Silent alarms
The 20:35 failure emailed a summary and stopped. Nothing paged. The monitors an
agent set up were ad-hoc `tail -F | grep` over ssh, and one of them **dropped
mid-event** and had to be restarted with an auto-reconnect loop. There is no
supervised, restartable, first-class run monitor.

---

## PART V — What "reliable" should mean

Concrete invariants to design toward. Each was violated at the time of writing.

1. **Every armed event records both tapes.** Audio and Kalshi book, automatically,
   with no operator memory involved. A run that cannot record must say so loudly
   and mark itself unauditable — never silently proceed.
2. **Every armed event is auditable afterward by one command.** `pull-box-state.sh`
   then `score-precision.py` should produce: precision (was the word said), Δrace
   (were we ahead), and fill accounting that agrees with the ledger.
3. **Order counting has exactly one authority.** Today `sent`, `send_ts`,
   `order_status`, and `probe_ledger.json` can all disagree. Pick one, make the
   others derive from it, and make disagreement a loud failure.
4. **Run state survives a restart.** Fired-word set, order history, and ledger must
   be append-only per event, not rewritten per arm cycle.
5. **Connections are supervised with venue-appropriate policy.** Redial budget and
   cadence derived from the venue's observed behaviour, not a default of 2.
6. **A dead lane pages a human.** First drop, not after N failures; phone, not
   email; with retry and escalation.
7. **One procedure, one document, executable end-to-end**, replacing the RUNBOOK
   diary. Ideally a single command per stage with a machine-checkable
   precondition, so "are we ready?" is answerable without reading 148 scripts.
8. **Host-portability is structural.** No absolute path from one host may be
   written into an artifact that executes on another.
9. **Liquidity is surveyed before arming**, and sizing is depth-aware.
10. **Staging is complete at stage time.** No placeholder (`starts_at`, TBD
    passcode, unset cap) may survive into a preflight PASS.

---

## PART VI — Suggested work order

Ordered by *(cost of the failure it prevents) ÷ (effort)*. Not prescriptive.

**Tier 0 — stop losing evidence** (nothing else can be measured without this)
1. Book tape wired into every armed event, with growth-based liveness (§III.5).
2. Audio tape for the **phone** lane (webcast was wired 2026-08-11).
3. Single order-accounting authority; make `sent`/`send_ts`/ledger agree or fail (§IV.2).
4. Append-only per-event run state so restarts stop erasing history (§IV.3, §IV.4).

**Tier 1 — stop losing events**
5. Auto-detect the drop cadence, classify operator-screened in-run, page the cell
   on first drop with retry (§III.3).
6. Venue-derived redial policy; kill the default of 2 (§III.2).
7. Failure notifications that page rather than email (§IV.12).
8. Purge host-absolute paths from generated artifacts; add a preflight gate that
   stats every referenced file **on the host that will run it** (§IV.1).

**Tier 2 — make the procedure real**
9. Replace the RUNBOOK diary with one executable procedure (§III.4). Archive the
   diary; it is valuable history, but it is not a runbook.
10. Collapse the 148 scripts. Many are one-event artifacts
    (`hims-26aug10-sweep-only.sh`, `stage-afternoon-26aug04.py`,
    `restudy-26aug04.sh`, `counterfactual-26aug04.sh`). Identify the ~20 that are
    the actual lifecycle and retire or clearly quarantine the rest.
11. Fix `missed-arm-check.py` config-set selection and its "excuse a zero" logic (§IV.5).
12. Fix the admission probe window for T-30-opening venues (§IV.8).

**Tier 3 — deliberate money decisions** (need the operator, not just an engineer)
13. `book_stale_enforce` and a realistic limit (§IV.6).
14. `MENTION_FILL_CAP_CENTS` 97 vs 99, given two observations of depth at 98–99¢
    and a measured 9/9 precision (§III.1).
15. Ledger scope: per-event vs truly cumulative, and document whichever is chosen (§IV.7).
16. Depth-aware sizing.

---

## Appendix A — Where things live

```
src/                    17.3k LOC Rust runtime
  main.rs               CLI + `arm` composer (execution side)
  pipeline/mod.rs       `run_pipeline` / `run_pipeline_fanout` (audio composer)
  source/               twilio_ws | pipe | replay | sip | arbiter
  detector/             port + factory, streaming_ctc, sherpa_kws, kws_compile
  decision.rs           one-shot decision table
  kalshi/               book (WS cache), executor, rest, auth, validator, fix/
  risk.rs               caps, ledger, kill switch  ← all money constants
  clock.rs              MonotonicNs — the only latency clock
architecture/nodes.toml 47-node ownership map (enforced by cargo test)
docs/ARCHITECTURE.md    the design; still accurate
docs/NODES.md           human index of nodes
docs/nodes/*.md         36 per-node contracts
docs/layers/*.md        13 runtime leaf contracts
docs/RUNBOOK.md         ~2,240-line event diary  ← the thing to replace
VERIFICATIONS.md        dated, evidence-backed wire facts (Q-ADMIT, Q-Q4PHONE, …)
configs/                165 toml — templates, per-event arms, raw prepare output
  .readiness/           study artifacts (keywords_sha256)
  .acks/                explicit operator acknowledgements
  dial-ins.toml         venue registry — admission verdicts live here
scripts/                148 scripts — the operational layer
state/<EVENT_ID>/       per-event run artifacts (gitignored, NOT synced by deploy-box)
  box/                  pulled from the box by pull-box-state.sh
sweep-hft/              SEPARATE PROJECT — different strategy, shares only the
                        Kalshi + PM accounts. Do not touch from earshot work.
```

## Appendix B — Vocabulary

- **Mention market** — binary contract: will X say word Y on the call.
- **Lane** — an audio path for one event: phone or webcast.
- **Arm** — a live run that may place orders.
- **Posture** — per-word cap + size. *probe/OBSERVE* (1¢, cannot fill) vs *FILL*.
  Note: `max_price_cents = 1` is **not a 1¢ bet, it is a $0.00 bet** — nothing
  rests at 1¢, so the order is non-crossing and takes no position.
- **Green gate** — `assert-ready.sh`: preflight + readiness both current.
- **Tranche** — contract count for one order (10 = the high band).
- **LOST_RACE** — an intent that never reached the wire because nothing was
  offered at or under the cap. **Not** necessarily a speed loss.
- **Δrace** — how far ahead of the market's reprice we fired. Needs a book tape.
- **Q-*** — verification pins in `VERIFICATIONS.md` (Q-ADMIT admission classes,
  Q-Q4PHONE the Q4 bridge, Q-NGROK, Q-TLS, …).
- **k≤2 / short token** — a keyword compiling to ≤2 BPE tokens. Assembles from
  partial audio at near-ceiling confidence; no confidence gate can reach it. The
  study measures *recall*, so a high ratio on these is actively misleading.

## Appendix C — The 2026-08-11 timeline, as a worked example

Useful because every failure class appears once.

```
15:00Z  Configs found with starts_at = midnight UTC → would have missed both calls
        Q4 REST twin gives real time 21:00Z + an undocumented dial-in block
15:05Z  Bridge probe: IVR answers, asks for Meeting ID
15:11Z  Probe with the PUBLIC attendee-URL number → issuer's own event named back
        (inferred: Meeting ID is sufficient — WRONG, see 21:11Z)
16:00Z  Posture reworked twice (ratio tiers → two bands $10/$1); ledger 2000→7000→13000
16:30Z  Box: disk reclaimed, commit, rebuild, preflight PASS, green gate PASS
20:35Z  Timers fire → ACCOUNT_ENV points at a laptop path → 25 dials, rc=2, DEAD
20:44Z  "max attempts reached", summary EMAILED, nobody reading
20:57Z  Failure noticed by a human question, not by an alarm
20:58Z  Fixed and relaunched; both lanes live 2 min before the call
21:01Z  Bridge drops every ~74-90 s; redials were capped at 2 → raised to 40/60
21:11Z  Mid-call probe transcribes the truth: a PASSCODE is required; we are in
        the operator queue. CRWV: 0 detections all night. Phone lane never worked.
21:13Z  Webcast lanes armed instead — the only lane that could hear the calls
21:15Z+ 11 intents; 5 fills at $9.70; 1 miss by ONE CENT (98¢ vs 97 cap);
        rest LOST_RACE on empty books, some 245 s stale
22:02Z  Calls end. NO AUDIO WAS RECORDED — the five fills cannot be audited.
22:10Z  Grader excuses CRWV as "observing by design" using a config never armed
```

Total spend **$52.38**: five legitimate $9.70 fills on live-call detections, plus
$3.88 on `a i` firing four times on a hold announcement. Nothing had settled at
time of writing. HIMS 2026-08-10, scored the same evening, was **9/9 precision** —
so the detector is not obviously the weak part. The plumbing is.
