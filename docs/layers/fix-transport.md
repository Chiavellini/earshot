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
- **Mechanism:** `OrderTransport::Fix` arm calls
  `FixTransport::send_order(&template, &client_order_id)`
  (`src/kalshi/fix/transport.rs`).
- Selected by config `[kalshi] transport = "fix"` (+ optional `[kalshi.fix]`
  section); `main.rs::arm` connects + logs on at arm time, fail-fast.

## Exit door
- **Wire:** one 35=D NewOrderSingle per send on the persistent session
  (integer-cent price tag 44, IOC 59=3, STP 2964=1 — F6). The template's only
  fire-time holes are MsgSeqNum, SendingTime/TransactTime, BodyLength,
  CheckSum (`FixOrderTemplate::assemble`, µs-class into reused buffers).
- **Type:** the first TERMINAL ExecutionReport (35=8) correlated by ClOrdID
  (tag 11 == the REST `client_order_id` — same idempotency key on both
  wires), surfaced as `session::OwnedExecReport` → mapped into
  `transport::TransportAck` (status normalized to the REST ack vocabulary:
  filled/partial/canceled/rejected/expired). Non-terminal ERs (PendingNew/
  New) are held, never resolve the order.
- No ER within `er_timeout` → `KalshiError` → the executor's ambiguous path
  (REST `order_by_client_id`) resolves it — the dual-wire safety net, proven
  in `tests/exec_fix_mock.rs`.

## Obligations
- **Sequence order IS wire order**: every seqnum allocation
  (`SessionMachine::alloc_seq` via `send_app`/actions) writes its message to
  the socket before the single `Io` mutex is released. Never allocate a seq
  without immediately sending under the same lock.
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
  presign step); `hdrs_ts` = ER receive instant, `ack_ts` = parse complete.
- FIX shares REST's rate-limit token buckets (F10) — the wire buys latency,
  not throughput.

## Failure policy
- Unparseable inbound / checksum mismatch / seq violation → Disconnect +
  reconnect supervisor; in-flight orders fail their oneshot → callers hit the
  executor's ambiguous path. Never a blind resend at this layer.
- Disconnected at fire time → immediate `KalshiError::Ws` (executor handles).
- Logon refused / timed out at arm → `arm` fails fast, nothing armed.

## How to replace or edit this layer
- `src/kalshi/fix/{mod,encode,parse,session,seqstore,transport}.rs`; the
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
  signature: fill correlation, reject mapping, ER-timeout, TestRequest echo.
- `tests/exec_fix_mock.rs` — the executor end-to-end through the FIX arm +
  the REST ambiguous-path fallback.
- **Live (pending access, in order):** demo-env logon (`fix.demo.kalshi.co`)
  resolving the Q-FIX open questions → prod logon → 1¢ probe-order under the
  $1 ledger → FIX-vs-REST send→ack A/B from the same box.
