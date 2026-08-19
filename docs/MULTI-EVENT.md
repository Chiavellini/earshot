# Concurrent multi-event

**Status: two-account isolation implemented (code + unit tests); live run still
gated on M3 + the Twilio hold.** This doc answers: if two (or more) earnings calls
happen at the *exact same time*, can EARSHOT execute on both concurrently — and how.
§1–§7 are the original single-account feasibility analysis (why it was "no" and the
shared-account ceilings); **§8 is the implemented design** for the case where we run
**two separate Kalshi + two separate Twilio accounts**, one per event. It cites code
by exact `file:line` so claims can be re-checked as the system evolves.

A live two-event run remains **downstream of M3** (single-call Twilio capture must be
live and validated first) and the mention-probe posture — see §7. What is built today
is the *isolation machinery* (§8) that makes such a run cross-wire-proof once the
vendor prerequisites clear.

---

## 1. Verdict

**No — not today.** One `earshot arm` invocation = one config = one `[event]` = one
full audio→execution chain, start to finish. The audio *pipeline* itself scales
cleanly (it owns no global state), but the **operator around it is
single-event-per-process**, with shared filesystem singletons and fixed ingress
resources that two events would collide on. On top of that, both vendors add hard
external ceilings that no amount of our code can remove.

Note the distinction that matters: **multi-*market* within one event is already fully
supported and concurrent** — a single event fans out across many `keywords[]`, each
its own market ticker, over one multiplexed WS socket and a `JoinSet` of concurrent
order sends. What is *not* modeled anywhere is two **distinct events** (two different
calls, two different `[kalshi] series`) at once.

---

## 2. What already scales (the good news)

The hard part — the real-time audio hot path — is already multi-instance-safe by
construction:

- **`run_pipeline` owns zero global state** (`src/pipeline/mod.rs:22`). Each call
  allocates its own SPSC rings, spawns its own three OS threads
  (`preproc`/`chunker`/`detect`), and builds its own private current-thread tokio
  runtime to drive the source. N concurrent instances = N independent pipelines with
  no aliasing.
- **No mutable singletons anywhere in `src/`** — no `static mut`, `OnceLock`,
  `lazy_static`, or `thread_local` shared state. Every stateful object (`RiskGate`,
  `BookCache`, `OrderRegistry`, `DecisionTable`) is a per-instance value the caller
  constructs.
- **Execution already fans out concurrently within one event.** `run_executor`
  (`src/kalshi/executor.rs`) drains the decision-intent channel onto a `JoinSet`,
  so orders across different markets fire in parallel (the "Powell burst" case). Market
  data for every ticker is multiplexed over **one** WS connection (`run_ws`,
  `src/kalshi/book.rs`).

So the pipeline is not the obstacle. The obstacle is everything the single-event
*operator* (`arm`) provisions once and assumes it owns alone.

---

## 3. The single-event chokepoints (our code)

Each of these is a place where the code assumed "one event per process." None is an
architectural wall; each is a shared-resource choice that becomes a hazard the moment
a second event exists. **These were the chokepoints as the single-account analysis
found them; §8 resolves them for the two-account design.** Citations below are kept
current, with a `→ §8` marker where the item is now addressed.

- **`state/` directory (now parameterized → §8).** The original operator hardcoded the
  state path in `arm`; the multi-event change replaced it with the `--state-dir` flag
  (`state_dir: PathBuf` on `Command::Arm`, threaded through `arm` in `src/main.rs`), so
  each event owns its ledger/STOP/rules. A bare `Path::new("state")` now survives only
  in the standalone `probe_order` / `kill_drill` ops commands.

- **`probe_ledger.json` cross-process race — the original correctness hazard (→ §8).**
  The $1.00 cumulative real-money cap (`PROBE_LEDGER_CAP_CENTS`, `src/risk.rs`) is
  file-backed. `RiskGate::new` loads it into an in-memory `Ledger` under a `Mutex`
  (inside `LedgerStore`, `src/risk.rs`); `check_order` reads that in-memory total and
  `record_send` debits it in memory and pokes a `Notify`, with the disk write coalesced
  off the fire path by a background persister (`spawn_persister`) plus a `Drop` flush.
  *Within* one process this is safe (totals are monotonic, so concurrent persists are
  last-write-safe). But two processes sharing ONE file each load the same starting value,
  never see each other's debits, and their flushes are last-write-wins → the $1.00 cap
  can still be breached. Mooted by the two-account design: each account gets its own
  `--state-dir` ledger (§8).

- **~~Event-notional cap silently doubles.~~ RESOLVED 2026-08-18 by removal, and the
  concern inverted.** There is no per-event notional cap any more: `EventNotional`,
  `max_event_notional_cents`, `HARD_MAX_EVENT_NOTIONAL_CENTS` and the per-`RiskGate`
  `event_spent_cents` accumulator were all removed (operator ruling **O8**). The
  per-process doubling this entry described cannot happen because the per-process budget
  no longer exists. What replaces the worry: an event's spend is now bounded only by
  `max_order_notional_cents` × the one-shot table (one order per FILL keyword) and
  `PROBE_LEDGER_CAP_CENTS`, and concurrency multiplies THAT. Two processes = two
  independent probe ledgers, and there is still **no account-level ceiling anywhere**
  — see **O1** (`daily_loss_cap_cents` parsed, clamped, never read) and **O7**
  (a second system trades the same boards on this account).

- **`OrderRegistry` has no event key.** It is a flat `Arc<Mutex<Vec<String>>>` of
  server order ids (`src/kalshi/mod.rs:57`), and its own doc comment states the ids are
  "created BY THIS PROCESS" (`src/kalshi/mod.rs:52-55`). Built once per `arm`
  (`OrderRegistry::default()` in `main.rs::arm`). If two events shared one registry, a
  kill for event A would cancel event B's orders too — you could not kill one event
  without killing the other. (Separate processes → separate registries; §8.)

- **`STOP` kill file (now per-event → §8).** The watcher polls `state_dir.join("STOP")`
  (`spawn_kill_watcher`, `src/risk.rs`, wired in `main.rs::arm`). With one shared `state/`
  a single STOP — or one SIGINT — tripped *every* event; under `--state-dir` each event
  has its own `state/<id>/STOP`.

- **Fixed audio ingress.** `TwilioWsSource` holds one `TcpListener` and serves
  exactly ONE stream: it accepts until the first connection completes the WS
  handshake (failed handshakes are dropped and it keeps waiting — 2026-07-14
  hardening), then never accepts again, so a second Twilio dial to the same
  port is never serviced as a second source. Every checked-in config
  binds `127.0.0.1:8080` (`configs/twilio-live.toml:41`), fronted by one ngrok tunnel,
  dialed from one `TWILIO_FROM_NUMBER`.

- **The trap: `SourceArbiter` is NOT a multi-call mechanism.** It is dual-dial-in
  fan-*in* — "two carriers dial the *same* earnings call" (`src/source/arbiter.rs:1-18`)
  — and it cross-correlates the legs to dedupe into one clean stream. Feeding it two
  *different* calls would fail to align and corrupt the output. It cannot be repurposed
  for two events.

---

## 4. External ceilings (cannot be fixed in our code)

Even with every chokepoint above resolved, two vendors cap concurrency:

### Kalshi — one account, one credential, one rate bucket
This section describes the **single-account** posture. Per-event credential binding
(§8) lets each event carry its own account/credential, which is what removes these
ceilings for the two-account design.
- **Single credential (single-account posture).** `Signer::from_env()` reads one
  `KALSHI_API_KEY_ID` +
  private key (sourced from `~/.kalshi/earshot_env`). One key can place orders on any
  number of markets — but the *account* is single, so all rate limits and the pool of
  ~53 unrelated resting orders are account-wide.
- **Rate limit is a per-account token bucket, not per-market** (`VERIFICATIONS.md` Q2):
  Basic ≈ 10 order-creates/s, Advanced ≈ 30/s (free, self-serve upgrade); each create
  costs 10 tokens, cancel 2. Two simultaneous bursts contend for the *same* bucket —
  there is no per-market isolation to protect one event from the other's spike.
- **No client-side rate limiter exists today.** The executor fires all intents
  concurrently via `JoinSet` (`run_executor`, `src/kalshi/executor.rs`) and relies on
  server-side 429s (which carry no Retry-After). N events multiply write load against the
  one shared bucket with nothing coordinating them. (Two separate accounts = two separate
  buckets, so this contention does not apply to the two-account design — §8.)

### Twilio — outbound fraud hold (RESOLVED for account A, 2026-07-14)
- **The fraud/compliance hold on the primary account was LIFTED** — confirmed
  by live probe 2026-07-14 (`scripts/twilio-status.sh --probe` → call accepted).
  Ticket #28111133. Any NEW second account for multi-event should expect the
  same review hold and probe it the same way before relying on it.
- **Inbound does not sidestep it.** EARSHOT places an *outbound* call and ingests its
  `inbound_track` (the conference audio coming back). The hold sits exactly on the
  outbound origination the system depends on; there is no inbound-DID path.
- Beyond the hold: trial-account restrictions (verified numbers + spoken preamble),
  one `From` number in the tooling, and ngrok free-tier hostnames that rotate per
  session (`docs/TWILIO.md:80-95`).
- **Open unknown:** the repo has done *no* analysis of Twilio account-level
  concurrent-call / CPS limits or multi-number provisioning. Confirm directly with
  Twilio before relying on N simultaneous outbound calls.

---

## 5. Recommended upgrade path — multi-process

**One `arm` process per event, launched by a thin orchestrator.** This is the least
invasive path: it keeps the clean per-instance pipeline entirely untouched and
isolates most shared state naturally at the process boundary. Required changes
(described, not implemented):

1. **Parameterize `state/` per event** *(implemented — §8)*. Add a `--state-dir` flag
   that threads into `arm` and `RiskGate::new(cfg, state_dir)` (which already takes a
   `&Path`, `src/risk.rs`), so each process gets its own ledger, STOP file, and `rules/`.
   This isolates the kill switch and STOP per event for free.

2. **Add a shared account-level guard** for the two things processes *cannot* safely
   split at the filesystem boundary:
   - **The probe ledger** — needs a single owner or a cross-process advisory lock
     (e.g. `flock` on `probe_ledger.json` around the read-modify-write in
     `record_send`/`check`) so the $1.00 cap holds account-wide.
   - **The Kalshi per-account rate bucket** — a shared client-side token-bucket (a
     small broker both processes call, or a shared lock file) so two bursts don't blow
     the ~30 creates/s ceiling together. Also decide an **account-level** event-notional
     ceiling so two $2,000 budgets don't sum to $4,000 of unbounded exposure.

3. **Distinct ingress per event** — distinct bind ports, distinct ngrok tunnels /
   `TWILIO_STREAM_URL`, and distinct configs (one `[event]` / `[kalshi] series` each,
   already the config shape per `EventConfig`, `src/config.rs`). The `bind` is validated
   only as a socket addr (`validate`, `src/config.rs`), so different ports Just Work.

4. **Vendor prerequisites** — upgrade Kalshi to the Advanced tier for burst headroom;
   get Twilio off the fraud hold, upgraded past trial, and (per the §4 open unknown)
   confirm the account permits the needed concurrent outbound calls with enough `From`
   numbers.

---

## 6. Alternative (noted, not recommended now) — single-process multi-event

Add an explicit `event` dimension to `OrderRegistry`, `RiskGate` accounting, and
kill-switch scoping, and run N pipelines inside one process. This gives cleaner
risk/rate accounting in one place — no cross-process ledger race, and a natural home
for a single shared rate limiter — but it is a deeper rewrite of the execution core
(the registry, the risk gate, and the kill closure all currently assume one event).
Revisit only if process-per-event orchestration proves fragile in practice.

---

## 7. Sequencing / gating

Multi-event sits behind two milestones, in order:
1. **M3 live** — single-call Twilio dial-in capture working and validated (blocked
   today by the outbound fraud hold).
2. **Mention-probe posture** — the 1¢ verification run on a real call.

Only then does concurrent multi-event become the relevant next step. §8 is the
implemented isolation machinery; the live run is what waits on the gates above.

---

## 8. Implemented design — two accounts, one process per event

With a **second Kalshi account and a second Twilio account** (one per event), the
shared-account hazards of §3–§4 stop existing: each event has its own rate bucket, its
own $1 probe ledger, its own resting-order pool, its own `From` number. The remaining
job is pure **isolation** — making it *impossible* to wire event A's audio to account
B, and vice versa. That is now enforced at two independent layers.

### Layer 1 — cryptographic separation (structural)
- **Kalshi:** `auth.rs`/`rest.rs` hold zero global state; each `RestClient`/`Presigner`
  owns exactly one `Arc<Signer>` (`RestClient` `signer` field, `src/kalshi/rest.rs`;
  `Presigner`, `src/kalshi/auth.rs`; see `docs/layers/auth.md`). An order for A
  physically cannot be signed by B's key.
- **Twilio:** two distinct `TWILIO_AUTH_TOKEN`s make the WS signature check
  (`src/source/twilio_ws.rs:197`) *reject* a stream signed by the wrong account with a
  403. (With one shared account the signed string is just the URL and cannot tell A from
  B — two accounts turn ingress into a real crypto boundary.)

### Layer 2 — config-declared identity + fail-fast (this change)
Credentials are no longer ambient process-env; each event's config *names* its account,
and `arm` refuses to start on any inconsistency.

- **`[kalshi]` gains `key_id` + `private_key_path` (+ optional `expected_key_id`
  tripwire)** — `src/config.rs` `KalshiSection`. When set, `connect_live_for`
  (`src/main.rs`) builds the signer via `Signer::from_pem` from THIS config, never
  `from_env`. The signed `balance()` self-test then proves the (key_id, key) pair is a
  coherent, live credential — a mismatched key file fails here and refuses to arm.
  Fields are **optional**: absent → env fallback, so every existing single-account
  config still loads.
- **`[twilio]` section (`auth_token_path`, `stream_url`, optional `account_sid` /
  `from_number`)** — `src/config.rs` `TwilioSection`. When present, `resolve_twilio_auth`
  (`src/main.rs`) reads the token from the file and `build_leg` pins it + the exact
  `stream_url` into the source via `TwilioWsSource::with_auth`, so the signature base is
  never reconstructed from the client `Host` header. Optional → env fallback.
- **`--state-dir` flag on `arm`** (default `state/`) — gives each event its own
  `probe_ledger.json`, `STOP`, and `rules/`. With two accounts this fully isolates each
  event's $1 ledger and kill switch: the cross-process ledger race of §3 is gone (each
  account writes its own ledger under its own dir), and `touch state/<event>/STOP` kills
  only that event.

### Orchestration — `scripts/launch-event.sh`
One invocation = one isolated event. It sources that event's `--account` env file (the
single source of truth for its Kalshi + Twilio secrets) into its own process env, starts
that event's own ngrok tunnel on its `--bind-port`, exports the matching
`TWILIO_STREAM_URL`, arms with `--state-dir state/<event-id>`, waits for the arm's
readiness token `state/<event-id>/listener.ready` (written only once the detector is
built and the pipeline is accepting — a bare `nc -z` port check lies here, because the
WS listener binds before the detector build; cost the whole phone lane on 2026-07-29),
then dials via `scripts/twilio-dial.sh`. Run it twice (two configs, two account files, two ports) for
two isolated events. Because each process sources a *different* account file and the
config binds the Kalshi key + Twilio token to that account, the human glue cannot
misalign env, port, tunnel, and stream URL across events.

### What this does and does not guarantee
- **Does:** an armed event can only sign Kalshi orders with the key its config names and
  only accept a Twilio stream signed by its account's token; ledgers and kill switches
  are per-event; a config whose `key_id`/`expected_key_id` disagree, or whose key file
  doesn't match, **refuses to arm**.
- **Does not (deliberately, per the chosen tier):** independently prove a key belongs to
  a *specific expected* Kalshi account beyond "it is a valid account that can sign" — that
  is the remote identity-probe tier (would capture and assert an account id from a REST
  call, needing the stable field pinned in `VERIFICATIONS.md` first). Nor does it gate
  ingress to a *specific CallSid* (the two distinct auth tokens already keep accounts
  apart; an `EXPECTED_CALL_SID` gate in the `start` handler is the next tier up).

### Performance — does a second process slow execution?
No, not on the current box (Apple M2, 8 cores, 8 GB). Each event has exactly **one**
CPU-hot thread — the detect/inference thread, `num_threads = 1` by default, so inference
does not fan out — plus ~5 near-idle network-bound threads. Two events = two busy
inference threads on eight cores, with six to spare; no oversubscription and no added
detect-thread jitter (there is no core pinning — deferred to M5 — but at 8 cores the
scheduler has ample headroom). Execution latency (detect→order) is per-process and does
not cross events, and two separate Kalshi accounts mean two separate rate buckets, so
there is no API-side contention either. **Guardrails:** keep detector `num_threads = 1`
per process (raising it lets one event grab multiple cores and oversubscribe), and watch
8 GB RAM if both events run the heavier `streaming_ctc` ONNX model (footprint unmeasured).

### Readiness checklist — receiving a second account
- [ ] 2nd Kalshi credential provisioned → config `[kalshi] key_id` + `private_key_path`
      (paths only; secret PEM stays out of the repo).
- [ ] 2nd Twilio account → its own account env file (`TWILIO_ACCOUNT_SID`/`AUTH_TOKEN`/
      `FROM_NUMBER`) + config `[twilio]` (`auth_token_path`, `stream_url`).
- [ ] Two configs with **distinct** `[[sources]] bind` port, `[event] id`, and
      `[kalshi] series`.
- [ ] ~~Two ngrok accounts (free tier = one agent each) → pass each event's auth file via
      `launch-event.sh --ngrok-config <acctX.yml>`.~~ **REFUTED 2026-07-29
      (VERIFICATIONS Q-NGROK):** the free plan serves ONE static endpoint per
      account regardless of config, so concurrent phone events on one account are
      impossible, and a second free account is a policy risk. The supported path is
      TLS-on-box: `launch-event.sh --tls-host media.<domain>` gives each event its
      own `wss://media.<domain>/e/<bind-port>` through the caddy ingress
      (`deploy/caddy/Caddyfile`) — no tunnels, no per-account cap. Each event still
      pins its exact `stream_url` (the signature base) via its `[twilio]` section.
- [ ] Both Twilio accounts off the fraud hold + past trial; both Kalshi accounts ideally
      Advanced tier (§4). **Live run still gated on M3** (§7).
- Report/state isolation and the ngrok per-event web-API port are handled by
  `launch-event.sh` (report → `<state-dir>/report.json`; ngrok `--web-addr` per event).
- Not covered by `arm`'s `--state-dir`: the standalone `probe_order` / `kill_drill` ops
  commands still use the shared `state/` — don't run them next to a live event.

### Still external, still gated
Two accounts remove the *shared*-account ceilings but not the vendor prerequisites: each
Twilio account must be off the fraud hold and upgraded past trial (`docs/TWILIO.md:82-88`),
and each Kalshi account should be on the Advanced tier for burst headroom (§4). A live
two-event run waits on M3 (§7).
