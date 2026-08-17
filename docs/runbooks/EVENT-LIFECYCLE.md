# EVENT-LIFECYCLE — the one procedure every event follows

This is the current, generic operating procedure. Dated runbooks are historical
examples; `docs/history/RUNBOOK.md` is a diary. Neither may override this file,
`architecture/operations.toml`, an immutable RunPlan, or a node contract.

The short answer is:

```bash
scripts/opsctl.sh prepare-day --date YYYY-MM-DD
scripts/opsctl.sh status YYYY-MM-DD
```

`prepare-day` always runs the same five aggregate stages—stage, access, study,
preflight, schedule. It keeps ready sibling events moving when one event is
blocked, but returns nonzero if any stage reports a problem. A failed run is
never marked scheduled. Use `scripts/opsctl.sh surface` to list every supported
operator command. Launchers, proof writers, and OS installers are internal
adapters and must not be hand-assembled at T-0.

## 1. One-time host/account setup

Provision each external account environment once, naming the stable host that
will execute it:

```bash
scripts/execution-profile.sh configure \
  --account '$HOME/.earshot/<account>.env' \
  --host-id <stable-target-host> \
  --bin target/release/earshot
```

The command derives `sha256(key-id || NUL || RSA-public-DER)` through the same
local signer selector as `arm`; it makes no network request and stores no
credential in the repository. Generated event envs carry only a portable
profile locator. A RunPlan refuses if its target host or signer fingerprint no
longer matches.

## 2. Run identity and evidence boundary

One event lane attempt has one immutable directory:

```text
var/runs/<EVENT_ID>/<RUN_ID>/
  control/run-plan.json
  control/receipts.jsonl
  control/status.json                 derived, never authoritative
  control/access-plan.json            webcast only
  control/capture-ready.json
  control/pcm-proof.json               webcast only
  control/preflight-proof.json
  control/preflight-report.json
  control/schedule-proof.json
  capture/audio.raw
  capture/audio.frames.jsonl
  capture/audio.manifest.json         pending, not a commit point
  capture/audio.commit.json
  capture/audio.lock
  capture/wav/
  book/kalshi-tape.jsonl
  orders/order-evidence.jsonl          accounting authority
  orders/fix-observations.jsonl
  report/report.json
  grade/verdict.json
```

The default RunId is deterministic:
`<starts-at compact UTC>-p3-<config SHA-256 first 12>`. Repeating init for the
same bytes is idempotent; changing a config creates a new attempt. Existing
schema-1/2 attempts are immutable historical evidence and are never migrated in
place.

Every receipt is a canonical JSON line under `flock`, `O_APPEND`, one write,
and fsync. Its hash chain is anchored at the RunPlan hash. Config/env drift,
receipt corruption, sibling ambiguity, or a symlinked artifact fails closed.

## 3. Night-before preparation

Run the generic chain for the UTC event date:

```bash
scripts/opsctl.sh prepare-day --date YYYY-MM-DD
```

For a no-write preview of the sequence:

```bash
scripts/opsctl.sh prepare-day --date YYYY-MM-DD --dry-run
```

The five stages are:

1. `stage-nightly.sh` merges independently verified times, generates every
   lane config/env from the slate, and compiles schema-3 RunPlans. Placeholder
   times, ambiguous mappings, host-absolute generated paths, or identity
   failures are problems and make the stage nonzero.
2. `access-plan.py check-day` requires every active webcast RunPlan to carry
   an immutable schema-2 AccessPlan. Access is explicitly classified as
   public, registered-session, or signed-in-session. A missing prerequisite
   prints the exact generic `opsctl bind-access` action and keeps the date red;
   the system never guesses, registers, signs in, or stores cookies/tokens.
3. `study-nightly.sh` measures every RunPlan-pinned config in call order. A
   spelling proposal never mutates that pinned config: it is reviewed and, if
   adopted, produces a new config hash and new RunPlan attempt before another
   measurement. Measured non-crossing/unserved residue may be acknowledged; a
   phrase mismatch remains a descope decision.
4. `preflight-nightly.sh` runs the proof-bearing preflight for each staged env.
   Missing account identity is a failure, not a skip. A green result durably
   publishes its report and prerequisite receipts before `preflight_passed`.
5. `schedule-nightly.sh` enumerates RunPlans, not filenames. Only an active run
   with `preflight_passed` reaches an installer. A date with no RunPlans is a
   refusal, not a green no-op.

The supported explicit outcomes are: fix the failing event, descope it on the
record, or leave the date command nonzero. Silence is not an outcome.

## 4. What preflight proves

Webcast preflight consumes an immutable schema-2 AccessPlan bound to the
RunPlan, config, source argv, URL file/resolver, target host/vantage, deadline,
and an explicit satisfied public/session prerequisite. It records only a
non-secret session label, attester, and expiry when a human prepared a session;
cookies, tokens, login identity, and registration data remain external. It
explicitly records `current_media_proven=false`; only fire-time PCM can prove
current media. Historical schema-1 AccessPlans remain readable but are refused
by the canonical preflight/schedule path and are never rewritten in place.

Phone preflight selects one typed strategy:

- fresh probe-proven held;
- rehearsed operator-first;
- unknown/untested first-drop operator fallback;
- reservation-window fallback.

Stale, future, malformed, or merely inferred held evidence is never promoted.
Operator routing capability is verified by executable hashes and contract
markers. A configured page, bridge dial, or connected stream is not admission.

`--only-readiness` remains side-effect-free and cannot create lifecycle proof.

## 5. What scheduling proves

Scheduling is not “a plist/unit file exists.” The installer reads the job back
from the loaded OS scheduler, normalizes that readback, and passes it to the
shared schedule-proof boundary. The proof binds event, run, plan hash, stable
host, label, future fire time, exact argv/environment, and stop policy. systemd
must expose `TimeoutStopSec >= 150` and `KillMode=mixed`.

Only after `control/schedule-proof.json` is atomically published may the ledger
append `scheduled`. Verification is read-only and idempotent; a loaded job that
does not match rolls back/refuses rather than claiming coverage.
The installer, fire-time resolver, PCM gate, and preflight each independently
require canonical schema 2, so a legacy AccessPlan cannot bypass the date gate
through a lower-level adapter.

## 6. Fire-time and live-run invariants

- Exactly one scheduler job launches exactly one immutable RunPlan. The outer
  supervisor does not reconstruct the Rust arm after a source drop.
- Webcast proves current media and capture readiness at fire time before
  `launched`; phone proves listener/detector readiness before dialing.
- Both launchers require the runtime-owned, identity-bound `source.attached`
  marker proving post-preprocessor frames entered the in-process evidence path.
- Audio, book, and order evidence are mandatory for every live run. Raw FIX
  observation evidence is additionally mandatory when transport is FIX. A
  missing or unhealthy required writer cannot be downgraded to a warning or
  replaced by an opt-in sidecar.
- The phone held-admission watcher consumes each `(CallSid, attempt)` once and
  renews the current-stream receipt after a redial. Policy-closed venues remain
  closed; integrity faults assert STOP before paging.
- Known operator-screened venues start human-first. Unknown/untested venues
  switch and latch to the operator topology on the first qualifying early drop.
  Only an explicit current challenge/stream-bound assertion opens decisions.
- The operator topology makes two bounded 45-second calls to the configured
  cell. A failed page must have a valid CallSid and a proven hangup before
  retry; ambiguous origination or teardown fails closed. The bridge is dialed
  exactly once, only after the human joins. Its media fork continues to the
  execution box while decisions remain closed until the human explicitly
  asserts the current challenge and stream.
- STOP/TERM first reaches the launcher. The launcher creates STOP and waits for
  runtime cancellation/evidence/report finalizers. TERM and KILL are bounded
  escalation only; scheduler stop budgets must not undercut this drain.
- Completion requires a matching finalized runtime report with runtime success,
  all required evidence healthy, and no unresolved cancellation IDs. Otherwise
  the run records `failed`.

The old `run-arm.sh`, `arm-when-url.sh`, `ms-morning.sh`, and
`wing-autorun.sh` paths are intentionally inert and refuse with exit 64. Do not
restore them for a particular market; fix the generic stage/acquire/schedule
boundary instead.

## 7. Monitor, retrieve, and audit

Read the laptop's verified authority without mutation:

```bash
scripts/opsctl.sh status <EVENT_ID | YYYY-MM-DD | today>
```

For a phone run, dispatch transfers the authoritative ledger tail to the box.
Until a terminal pull succeeds, laptop status explicitly reports
`STALE_PREFIX_POSSIBLE`, returns nonzero, and must not be read as proof that a
remote launch completed. A future remote read-through can narrow that window;
it may not make a stale local prefix look authoritative.

After a box-hosted run reaches `completed`, `aborted`, or `failed`, retrieve it:

```bash
scripts/opsctl.sh pull --date YYYY-MM-DD
# or
scripts/opsctl.sh pull --event EVENT_ID
```

The puller stages and verifies every remote candidate before publication,
accepts only an exact ledger-prefix relation, refuses divergence/symlinks, and
publishes terminal runs only. Active/descoped runs remain inventory; they are
not merged into the laptop authority. `evidence_pulled` is appended locally
only after the stable snapshot is present.

### Closing or re-recording a DISPATCHED run (E11)

Once an installer has published a run's prefix on the box, the box mints the
next receipts (`scheduled`, `launched`, …). The laptop copy records that as
`ledger writes: REMOTE → <box>` in `eventctl status` and **refuses every
append** (`REFUSED: E11 …`) until a pull returns authority — appending
before the pull would fork the chain permanently. So, always:

```bash
scripts/eventctl.py authority --run-dir var/runs/<E>/<R> --show   # remote|local|unset
scripts/pull-box-state.sh --prelaunch --event <E>                  # pull: authority returns
scripts/eventctl.py record --run-dir var/runs/<E>/<R> --transition blocked   # now a clean extension
scripts/box-webcast-install.sh --run-dir var/runs/<E>/<R> --dispatch-only    # carry the close back
```

`schedule → pull → close → dispatch`, never `schedule → close`. After a
terminal run, `opsctl pull` returns authority the same way before it records
`evidence_pulled`. If the box is gone for good (instance terminated), the only
escape is `eventctl authority --run-dir … --reclaim --reason '…'`; it warns,
records the reason in the sidecar, and a later dispatch/pull still surfaces
any divergence the box had minted. Never hand-write `control/authority.json`,
and never copy it between hosts — it is this host's private marker.

Audit one retrieved run:

```bash
scripts/opsctl.sh audit --run-dir var/runs/<EVENT_ID>/<RUN_ID>
```

The audit independently verifies:

- order-evidence v4 transition/hash/accounting and every attempt, acceptance,
  fill, cancel, and unresolved request;
- runtime-report agreement with that authority;
- protocol-2 book commit and reconstruction of raw asks;
- no snapshot vs empty at detection vs available depth vs observed cap-depth
  exhaustion, with detection/wire monotonic timing;
- audio commit and WAV hashes.
- the independent raw FIX observation chain/commit when FIX is selected; REST
  must prove the typed not-applicable posture.

It never labels book movement a competitor sweep without evidence. Audio-backed
precision remains `not_resolvable` until transcript scoring runs. A successful
grade is byte-immutable and records `graded` only after evidence is
consolidated: `evidence_pulled` proves remote transfer, while a terminal
laptop-hosted webcast run records `evidence_consolidated` only after the same
audit has independently validated its local evidence in place.

Audit every attempt on a UTC date and reconcile outstanding alerts:

```bash
scripts/opsctl.sh audit-day --date YYYY-MM-DD
scripts/opsctl.sh alerts --run-dir var/runs/<EVENT_ID>/<RUN_ID>
scripts/opsctl.sh ack-alert --run-dir var/runs/<EVENT_ID>/<RUN_ID> \
  --alert-id <64HEX> --by <stable-operator-id> --note "seen/resolved"
```

Each critical run failure has one deterministic AlertRequest. Delivery attempts
and outcomes are durable lifecycle facts; phone attempts are bounded across the
alert lifetime, ambiguous calls are never retried blindly, and a local desktop
notification cannot satisfy the critical phone-delivery requirement. An alert
stays red until a human explicitly acknowledges it. `audit-day` continues
siblings but returns nonzero for an invalid run or an unacknowledged alert.

## 8. Lifecycle and recovery

```text
plan_compiled
  -> prepared ----------------------\
  -> admission_resolved (phone) -----+-> preflight_passed
  -> access_acquired (webcast) ------+
  -> study_completed ---------------/
  -> scheduled + capture_ready -> launched -> source_attached -> supervising
  -> draining -> completed -> evidence_consolidated -> graded -> archived

Pre-launch terminals: descope | blocked
Any-time terminals: aborted | failed
Remote retrieval also records `evidence_pulled`, a stronger consolidation fact.
Post-mortem tail after aborted/failed: evidence_consolidated -> graded -> archived
```

Repeated valid transitions are idempotent. A killed append may leave only an
unterminated final fragment; `eventctl verify` returns exit 3 and names the
exact safe truncation offset. A newline-terminated corrupt line or any other
mutation is a hard failure and must not be auto-repaired.

Useful lower-level read-only commands are:

```bash
python3 scripts/eventctl.py status --event EVENT_ID --json
python3 scripts/eventctl.py verify --event EVENT_ID
python3 scripts/access-plan.py validate --run-dir RUN_DIR --canonical
# schedule verification is performed by the lane installer against a fresh
# normalized launchctl/systemctl readback:
scripts/install-t0-agents.sh --run-dir RUN_DIR --expected-host HOST --verify-only
# phone/box equivalent:
scripts/install-event-timer.sh --run-dir RUN_DIR --expected-host HOST --verify-only
```

## 9. Change discipline

When a new venue or failure class appears, do not add an issuer/date launcher.
Change the owning node's generic contract, add focused offline tests, and add
the necessary typed fact to RunPlan/AccessPlan/preflight/schedule proof. The
operations manifest and doc lint reject unclassified dated or trade-capable
top-level scripts. Risk policy changes remain a separate, explicit operator
decision.
