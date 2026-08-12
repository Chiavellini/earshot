# Layer: fix-transport

## Role
The FIX order-entry wire (lever E2): a persistent FIXT.1.1/FIX50SP2 session
to Kalshi's matching-engine-adjacent FIX gateway, replacing the REST
CloudFront path for the fire-path send. Hand-rolled (no quickfix): encoder,
zero-copy parser, sans-io session machine, tokio TCP+TLS driver. NOT
responsible for: what/when to fire (decision/executor), risk (RiskGate is
strictly upstream), or the kill switch (stays REST — see Obligations).

**Status: offline build (2026-07-21).** Everything below is proven against
golden vectors and an in-process mock server ONLY. It has never touched a
live Kalshi FIX endpoint — access is Premier-gated (operator:
institutional@kalshi.com). Every wire fact + open question is pinned in
VERIFICATIONS.md §Q-FIX; the open questions MUST be resolved at the demo
logon before any live fire.

## Entry door
- **Type:** `executor::PreStaged` whose `wire` is `WireStaged::Fix { template:
  fix::encode::FixOrderTemplate }` (built at arm time via
  `StageWire::Fix(&FixConfig)` — see `src/kalshi/transport.rs`).
- **Mechanism:** the complete result door is
  `FixTransport::submit_order_traced(&template, &client_order_id)` and the
  immediate observation door is `FixTransport::subscribe_observations()`
  (`src/kalshi/fix/transport.rs`). A durable evidence adapter subscribes once
  at arm time, before any order can fire, then consumes each ordered envelope
  without putting disk work on the reader task. `OrderTransport::Fix` uses the
  additive `FixTransport::submit_order` compatibility result; it retains the
  terminal/latest outcome but not the full intermediate sequence. The oldest
  `send_order` facade additionally collapses failures back into `KalshiError`.
- A caller that requires crash-safe order evidence constructs the session with
  `FixTransport::connect_with_pre_write_hook(..., Arc<dyn FixPreWriteHook>)`.
  The hook is immutable for that transport instance; it cannot be detached
  after arm. The compatibility `connect` constructor installs no hook.
- **Raw receive banker (library door):**
  `observation_tape::FixObservationTapeTask::start` opens and exclusively locks
  one run-scoped JSONL tape, strictly replays its hash chain, and durably appends
  a process-session start before returning a bounded, nonblocking
  `FixObservationTap`. The tap accepts `FixObservationEnvelope` values; its
  writer syncs every distinct receipt before advancing durable accounting.
  `verify_fix_observation_tape` independently reparses each exact FIX frame and
  verifies typed projections, identities, hashes, receive sequence, duplicate
  pointers, session accounting, and close commits.
- **Mandatory transport seam:**
  `FixTransport::connect_with_mandatory_observer(config, signer, timeout,
  pre_write_hook, tap)` installs the banker before TCP dial and Logon. Any
  order-relevant frame coalesced with the successful Logon reply is banked
  before the reader can start; reconnects retain the same tap and strictly
  increasing process observation sequence. A tap refusal fails initial Logon
  or marks the live generation disconnected before correlated consumers receive
  the frame. Best-effort `subscribe_observations` remains additive and may be
  registered after connect, but cannot itself claim zero-gap capture.
- **Assembly integration status:** the banker and mandatory transport seam are
  implemented, but `main.rs` does not yet open the run-scoped artifact or call
  this constructor. A FIX arm therefore remains operationally unbanked until
  the RunPlan and assembly nodes wire the seam, health, close, and report.
- Selected by config `[kalshi] transport = "fix"` (+ optional `[kalshi.fix]`
  section); `main.rs::arm` connects + logs on at arm time, fail-fast.

## Exit door
- **Wire:** one 35=D NewOrderSingle per send on the persistent session
  (integer-cent price tag 44, IOC 59=3, STP 2964=1 — F6). The template's only
  fire-time holes are MsgSeqNum, SendingTime/TransactTime, BodyLength,
  CheckSum (`FixOrderTemplate::assemble`, µs-class into reused buffers).
- **Type:** `transport::FixTracedSendResult` is either `NotAttempted { error }`
  or `Attempted(FixTracedSendAttempt)`. `send_ts` is sampled immediately
  before entering `write_all`; therefore `NotAttempted` proves that no socket
  write was entered, while every attempted result is ambiguous until exchange
  evidence resolves it. `FixSendResult` is the compatible terminal/latest
  projection of this type.
- `FixTracedSendAttempt::observations` preserves every correlated New,
  Partial/Trade, and terminal ExecutionReport in receive order. Its
  `first_response_recv_ts` / `first_response_ack_ts` describe the first
  response, never the terminal response substituted as first-ack latency.
  `outcome` still resolves only on a terminal ER or an exactly correlated FIX
  reject. On timeout/disconnect the latest ER remains available through
  `FixAttemptFailure::last_observation`.
- **Immediate stream:** `FixObservationEnvelope { sequence, generation,
  observation }` is sent through an unbounded Tokio channel. `sequence` is
  strictly increasing process-local receive order across reconnects. Every ER
  is published, including an uncorrelated one; therefore the first New or
  Partial report exposes its exchange `order_id` immediately instead of
  trapping it behind the terminal wait. The channel send is nonblocking and
  performs no disk work. Durable sink lag/health is owned by its consumer.
- `FixOrderObservation` is either `Execution(FixExecutionObservation)` or
  `Reject(FixRejectObservation)`. Session Reject (35=3) and
  BusinessMessageReject (35=j) preserve exact framed bytes plus their reason,
  text, reference fields, receive/correlation timestamps, generation, and an
  exact client id when tag 45 maps to this generation's outbound sequence or
  tag 379 equals an in-flight ClOrdID. They no longer exist only in logs.
- `FixExecutionObservation::raw` likewise preserves the exact validated
  inbound ExecutionReport frame, including BodyLength and CheckSum. The same
  bytes survive both clones: the immediate observation envelope and the
  correlated traced submission result. `OwnedExecReport::raw` remains the
  parsed field projection and is not a substitute for this wire evidence.
- **Receive-tape schema:** protocol 1 uses canonical JSONL records
  `session_started`, `observation`, and `session_closed`. Every record repeats
  immutable run/event/environment/account/plan/endpoint identity and a
  cross-process SHA-256 chain anchored at `plan_sha256`. Observation records
  preserve envelope sequence/generation, MsgType/MsgSeqNum/tag 52,
  receive/correlation monotonic clocks, the typed execution/reject projection,
  and an all-or-none raw object (`application/fix; version=FIXT.1.1`, canonical
  base64, byte length, SHA-256). The record wall time is explicitly the banker
  host's persistence time, not the socket receive instant.
- A delivery receipt id hashes process session, envelope sequence, generation,
  and raw-frame digest. Reoffering that same receipt is idempotent and counted
  in the durable close row. `frame_key` separately hashes endpoint identity and
  raw-frame digest. A later distinct receipt with identical bytes is always
  appended and points to the first tape row: exact wire repetition is evidence,
  not something deduplication may erase.
- Clean producer quiescence appends and syncs `session_closed`, then publishes a
  create-new commit receipt bound to the exact tape prefix and close-record
  hash. A later process can append another session to the same run tape.
  Missing prior commits, recovered unclosed processes, queue loss, or incomplete
  sessions permanently make whole-tape completeness false without preventing
  later evidence from being banked.
- **Order-evidence adapter mapping:** the pre-write adapter parses tag 55 from
  `FixPreWriteEvidence::outbound_frame`, looks up the already-durable
  `SendRequested` by tag 11/client id, and records that exact frame as
  `RawEvidence` with protocol `Fix`. The inbound adapter uses the parent
  request's ticker (an ER is not required to echo tag 55), `recv_ts` as
  `observed_ts`, `report.raw` for the typed projection, and
  `FixExecutionObservation::raw` as the authoritative `RawEvidence`. It may
  derive exchange time from tags 60/52 in those bytes and fixed-point fill
  fields from the parsed projection; it must not round through `f64`.
- An ER or reject without an exact, already-journaled client id cannot be
  forced into an order lifecycle row: the order-evidence reducer requires a
  `SendRequested` parent. The adapter must persist such an observation in a
  session/orphan evidence artifact (or fail its health boundary) rather than
  silently drop it or attach it heuristically. Correlated 35=3/35=j rejects
  map to terminal rejected exchange observations with their exact `raw`
  frame; uncorrelated rejects follow the same orphan rule.
- The same separation applies to **late correlated** frames. The strict order
  reducer intentionally accepts no successor after a terminal fill, reject,
  cancel, expiry, `NotAttempted`, or terminal reconciliation. The raw receive
  tape remains authoritative even when a correlated observation is not a legal
  next order-state transition. An adapter may project only the reducer-legal
  subset and must never make that projection the sole copy.
- **Pre-write durability door:** `FixPreWriteHook::before_write` receives
  `FixPreWriteEvidence` only after MsgSeqNum/SendingTime/TransactTime,
  BodyLength, and CheckSum have been assigned, but before `send_ts` is sampled
  or `write_all` is entered. The evidence owns the exact framed bytes,
  connection generation, endpoint host/port/TLS and CompIDs, ClOrdID, assigned
  sequence/time, and `prepared_ts`. A journal adapter may await a dedicated
  writer acknowledgement here. A successful hook therefore proves the exact
  attempted frame was durable before the ambiguous socket boundary; a crash
  after that acknowledgement is deliberately treated as possibly attempted.
- `OwnedExecReport::raw` preserves the observed order id, status, cumulative
  quantity, leaves quantity, last fill quantity/price, average price, and the
  raw ExecType/OrdStatus vocabulary.
- `FixTransport::send_order` remains source-compatible and maps a terminal
  observation back to `(OwnedExecReport, send_ts, recv_ts)`. On any failure it
  returns only `KalshiError`, discarding the rich API's `send_ts` and latest
  non-terminal observation. It is a migration facade, not an evidence door.
- An attempted terminal-wait failure still enters the executor's REST
  `order_by_client_id` ambiguous path through `OrderTransport::submit_order`;
  the dual-wire safety net remains covered in `tests/exec_fix_mock.rs`.

## Obligations
- **Sequence order IS wire order**: the writer mutex is acquired before the
  session mutex for every path that can allocate an outbound sequence
  (`send_app`, heartbeat/TestRequest/Logout actions, reconnect install). The
  sequence is allocated and its message written before the writer door is
  released. ER/reject frames cannot allocate a response and may use the
  session mutex without the writer; this is what lets already-parsed exchange
  evidence survive a concurrent failed flush. If a write or flush fails after
  allocation, mark that generation disconnected and never allocate another
  application sequence on it.
- A configured pre-write hook runs while the writer serialization door is
  held but after the session mutex is released. It may await a nonblocking
  journal service; it must never do blocking filesystem work on the Tokio
  thread. Inbound ER/reject parsing remains independent and continues while
  the receipt is awaited.
- **Session discipline (KalshiNR posture)**: 141=Y reset-on-logon; answer
  TestRequest with a 112-echoing Heartbeat; answer ResendRequest with
  SequenceReset-GapFill (NEVER retransmit an application message); inbound
  seq-too-low or seq-gap is fatal → reconnect fresh (recovery exists only on
  KalshiRT, F9). Reconnect backoff floor 1 s (Logon is rate-limited, F10),
  cap 30 s.
- **Logon auth (F3)**: RawData (95/96) = base64 RSA-PSS/SHA-256 over
  `SendingTime SOH MsgType SOH MsgSeqNum SOH SenderCompID SOH TargetCompID`,
  signed with the SAME key as REST via `Signer::sign_raw` — a DIFFERENT
  canonical string than REST's `{ts}{METHOD}{path}`. SendingTime in the
  pre-hash must byte-match tag 52.
- **Kill-switch scope untouched**: the registry holds exchange `order_id`s
  and the kill path cancels them over REST regardless of entry wire (same
  order store). 35=F is encoded (`encode::order_cancel`) for completeness but
  is NOT wired into the kill path — never widen that scope.
- **Money**: prices are integer cents end-to-end (tag 44 is `u32` cents —
  never dollar strings; subpenny mode 21005 deliberately not enabled).
  Timestamps: `clock::MonotonicNs` for latency; wall-clock only inside
  SendingTime (`encode::utc_sending_time`, hand-rolled civil-from-days).
- **Telemetry honesty**: `SendOutcome.presign_age_ns = None` (FIX has no
  presign step); rich `recv_ts` = complete-frame receive instant and rich
  `ack_ts` = parse/correlation complete. First-response latency comes from the
  traced result or observation stream. The compatible normalized transport
  currently sees only its projected terminal/latest observation and must not
  label that timestamp as the first FIX acknowledgement.
- The receive banker never blocks the FIX reader on filesystem work. Its
  bounded `try_record` door poisons shared health on a full or disconnected
  queue and increments an exact drop counter. Runtime integration must treat
  that signal or premature writer completion as an immediate evidence fault:
  kill the gate, cancel only the session registry over REST, stop the producer,
  drain the accepted backlog, and refuse to mark the tape complete.
- Assembly monitors `FixObservationTapeHandle::subscribe_failures()` (or the
  cloneable `health()` handle) plus `is_finished()` / cancellation-safe
  `wait()`, just as it monitors the book recorder. A clean runtime branch must
  re-read health after `select!` so simultaneous pipeline completion cannot mask
  an already-published tape failure.
- FIX shares REST's rate-limit token buckets (F10) — the wire buys latency,
  not throughput.

## Failure policy
- Unparseable inbound / checksum mismatch / seq violation → disconnect that
  exact connection generation + reconnect supervisor. Reader and ticker tasks
  carry their generation: a late EOF or timer from an old socket cannot mark a
  replacement session disconnected or clear its pending map. All failed-
  generation channels close; each attempted caller returns its own `send_ts`
  and every observation already queued. An atomic guard permits at most one
  reconnect loop even when reader, ticker, and application write observe the
  same failure. Never a blind resend at this layer.
- Disconnected/session-not-established at fire time, or a duplicate in-flight
  ClOrdID, returns `NotAttempted` with no `send_ts`. Write/flush error after
  sequence allocation returns `Attempted`, marks only that generation
  disconnected, and closes only that generation's pending channels. Because
  ER/reject correlation does not wait on the writer mutex, every observation
  already parsed during the write/flush is queued before teardown; the caller
  drains the complete sequence. A terminal report remains success evidence, a
  correlated reject is a typed failure, and partial/trade reports remain in
  the trace and latest-observation compatibility field.
- Pre-write hook failure returns `NotAttempted` with no `send_ts` and never
  enters the socket writer. Because FIX sequence allocation already occurred,
  the generation is disconnected and re-established rather than emitting a
  sequence gap; its pending waiters enter their normal ambiguous recovery.
- Terminal success or an exactly correlated reject removes its pending ClOrdID
  when dispatched; timeout removes it before returning; connection failure or
  graceful shutdown clears only the owning generation. Timeout and report
  dispatch serialize on the pending-map mutex, then timeout drains every
  already queued report so a boundary-race terminal ack is not lost.
- Logon refused / timed out at arm → `arm` fails fast, nothing armed.
- Tape open/lock/replay/session-start failure is likewise an arm refusal once
  wired. During a run, write/sync/hash/sequence/queue failure poisons tape
  health. A graceful close is complete only when close was explicitly
  requested, every producer dropped, all accepted deliveries were durably
  accounted, no observation was dropped, every prior process session was
  complete, and the per-session commit receipt was published.
- Shutdown is ordered: drain the executor, request tape close, call
  `FixTransport::shutdown`, then drop **every** strong transport/`OrderTransport`
  owner so the mandatory tap sender quiesces, and only then await the tape
  handle and write the runtime report. `shutdown()` alone does not join the
  reader or drop the producer and therefore is not quiescence proof.

## How to replace or edit this layer
- `src/kalshi/fix/{mod,encode,observation_tape,parse,session,seqstore,transport}.rs`; the
  executor-facing seam is `src/kalshi/transport.rs`; wiring only in
  `main.rs::arm`; config in `config::{OrderWire, FixSection}`.
- The session machine is sans-io ON PURPOSE — new session behavior goes in
  `session.rs` with pure unit tests first, IO in `transport.rs` after.
- Wire facts live in VERIFICATIONS.md §Q-FIX — update with evidence (doc URL
  or live capture) before changing any tag/format; golden vectors in
  `encode.rs` must be recomputed independently (byte-sum, out-of-band) when
  the message shape changes.

## How to test
- `cargo test --lib kalshi::fix` — golden vectors (byte-exact 35=D +
  heartbeat with independently computed BodyLength/CheckSum), SendingTime
  epochs, Logon RawData signature verification, parser round-trips +
  corruption rejection, and the full session-machine suite (handshake,
  heartbeat/TestRequest liveness, GapFill, seq discipline, logout).
- `tests/fix_session_mock.rs` — FixTransport against an in-process mock FIX
  acceptor (plaintext `tls: false` seam) that validates the real Logon
  signature: terminal fill/reject, wire-attempt timestamp on timeout,
  ordered New→Trade(partial)→Trade(filled) preservation with immediate order-id
  visibility, typed/correlated Session and Business rejects,
  partial-then-timeout/disconnect evidence retention, pre-attempt disconnect,
  compatibility-wrapper behavior, and TestRequest echo. Transport unit tests
  deterministically fence a stale reader generation and inject a flush error
  after the reader has parsed partial+terminal reports. A separate injected
  journal-hook failure verifies the hook receives the exact parseable 35=D
  frame and endpoint identity while the writer's call count remains zero.
- `tests/fix_observation_tape.rs` — exact execution-frame banking and commit,
  orphan reject retention, same-delivery idempotence versus repeated-frame
  preservation, restart append, missing-prior-commit propagation,
  tamper/truncated-tail refusal, and exclusive writer ownership.
- `tests/exec_fix_mock.rs` — the executor end-to-end through the FIX arm +
  the REST ambiguous-path fallback.
- **Live (pending access, in order):** demo-env logon (`fix.demo.kalshi.co`)
  resolving the Q-FIX open questions → prod logon → 1¢ probe-order under the
  $1 ledger → FIX-vs-REST send→ack A/B from the same box.
