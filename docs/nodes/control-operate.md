# control.operate — event-day operations and reconstruction

## Job

Give the operator one place to run an event day from and one place to
reconstruct it afterwards. Born 2026-08-04, the day all 7 events failed on
human-memory steps and the evidence of *why* was spread across five
filesystem roots with no index. This node owns the day runbooks, the
per-event lifecycle journal, the green gate shared by every launcher, the
descope record, and the installers that turn "a human remembers at T-minus"
into loaded launchd agents. Since 2026-08-11 it also owns the typed run
control plane: one immutable run identity per event attempt and one
replayable, append-only lifecycle ledger under
`var/runs/<EVENT_ID>/<RUN_ID>/` (`scripts/eventctl.py`), born from the
2026-08-11 CAVA/CRWV reconstruction (`docs/history/casino143.md`) where $48.50 of fills
could not be audited and restarts kept overwriting fixed report paths.

## Owns

- `docs/runbooks/**` — the generic per-event procedure
  (`EVENT-LIFECYCLE.md`, the one document every event follows). The day-keyed
  historical examples (RUNBOOK-YYYY-MM-DD.md) and the historical accumulator
  `docs/history/RUNBOOK.md` are archived under `docs/history/`
  (evidence.measure) since 2026-08-12.
- `scripts/eventctl.py` — the run control plane: compiles the immutable
  `control/run-plan.json` (door `control.run.plan`) and appends the
  hash-chained `control/receipts.jsonl` (door `control.run.receipts`) under
  `var/runs/<EVENT_ID>/<RUN_ID>/`. Python stdlib only. It orchestrates
  existing node entry points and records what they did; it is NOT a third
  runtime composer — it never launches an arm, places a call, or touches
  order policy. Commands: `init`, `record`, `compat`, `status`, `verify`,
  `init-date`, `authority`. The default RUN_ID is deterministic
  (`<starts_at compact>-p<plan schema>-<sha256(config bytes)[:12]>`), so
  repeated identical `init` is idempotent, a config edit yields a NEW run
  identity, and a protocol upgrade cannot collide with or migrate an
  immutable older-schema attempt.
  **Ledger write authority (E11, 2026-08-17).** `control/authority.json` is
  this copy's host-private record of which host may currently extend the
  chain: `{authority: local|remote, execution_host_id, since_utc, by,
  receipts_count, receipts_sha256, reason}`. It is NOT a receipt (never in
  the chain), NOT an artifact destination (the plan's field set is
  exact-match), NOT transported (`run-host-boundary.py` ignores it on both
  sides; every rsync excludes it), and written ONLY by `eventctl authority`:
  `--transfer-to <execution host>` (installer door: the box now mints the
  next receipts), `--returned-from <execution host>` (puller door: the box's
  copy was just reconciled here), `--reclaim --reason …` (operator escape for
  a host that is gone for good; a later dispatch/pull still surfaces any
  divergence), `--show`. `Ledger.append` refuses a write while the sidecar
  says `remote` — after the transition gate has said "append" and before the
  first byte, so idempotent no-ops (nightly `init-date` re-compiles, a
  `record` of an already-reached transition) stay green and only a real
  write refuses. The refusal is one line naming the pull. `status` exposes
  `authority.write_authority/transferred_to/since_utc/by` additively.
- `scripts/opsctl.sh` — the public generic facade. `prepare-day` takes one UTC
  date and runs fresh public discovery, stage, canonical access checks, study,
  proof-bearing preflight, typed scheduling, and the final readiness audit in
  order. Each aggregate stage continues across sibling
  events, but the facade
  returns nonzero if any stage reports trouble; ready siblings proceed and a
  failed run is never called scheduled. Status, terminal-only box retrieval,
  deterministic run audit, and recorded descope are exposed by the same
  command. The facade contains no issuer/date map, credential, market posture,
  resolver, scheduler implementation, or runtime composer.
- `scripts/event-prep-audit.py` — the read-only whole-event join. It starts
  from every earnings event in the refreshed date slate, requires an active
  schema-3 attempt and all preparation/schedule proofs, verifies measured
  posture markers, and reads loaded launchd support jobs. A resolved direct-HLS
  webcast is not READY unless its pinned config contains both production
  survivor transports: master/media edge re-resolution and per-reload CDN
  cache busting. This applies to already-staged configs, not only the current
  template, so a vulnerable two-leg RunPlan must be replaced before scheduling.
  A preresolved event
  needs its PCM-gated push, a multi-host event needs its failover proof, and
  every event needs two watchdogs before the word READY is emitted. A watchdog
  label alone is not support proof: the audit reads each loaded job and requires
  the watchdog program plus every exact `<event_id>:<run_id>:` leg identity, so
  a job carrying a full run-directory string where the parser expects a Run ID
  is rejected before the event. Multi-host failover proof is likewise parsed,
  not existence-checked: its local path, box event/run, start time, launcher
  label, grace, and the loaded wrapper argv must all match the selected pair.
- `tests/eventctl_lifecycle.py` — the ledger's isolated test suite
  (temp-repo fixtures, never the working tree; includes cross-process
  flock/concurrency and truncated-tail recovery cases).
- `scripts/run-host-boundary.py` — the network-free authority-transfer
  primitive for one schema-3 run tree. A transport first lands a stable
  snapshot in a private staging path; this helper verifies it through
  `eventctl`, atomically publishes an absent canonical destination, or
  reconciles only when one receipt ledger is an exact byte prefix of the
  other. When the incoming host owns the extension, evidence is installed
  before the exact receipt suffix is appended under `flock` + `fsync`.
  Divergent ledgers, symlinks, unexplained destination files, and changed
  immutable artifacts are refused before receipt mutation. It performs no
  network operation. The host-private `control/authority.json` (E11) is
  invisible to it on BOTH sides — never published, never compared, never
  "unexplained" — so a stray copy can neither cross nor block a reconcile.
  `tests/run_host_boundary.py` exercises laptop-prefix →
  box-extension → laptop-tail round trips entirely in temporary repos,
  including the sidecar's non-transport.
- `scripts/execution-profile.sh` — the one-time external account-profile
  provisioner and read-only plan-time resolver. `configure` requires an
  explicit stable target-host ID, sources only the named account env with
  xtrace/output suppressed, calls the pinned binary's no-network
  `account-fingerprint` command, and atomically maintains a marked block with
  `EARSHOT_EXECUTION_HOST` plus
  `EARSHOT_KALSHI_ACCOUNT_FINGERPRINT`. `resolve` re-derives the exact signer
  selected for a given config and emits one non-secret identity JSON object
  only when it still matches. It never infers `hostname`, sources a generated
  event env, or contacts Kalshi. `tests/execution_profile.sh` uses only a temp
  HOME, temp account env, and fake binary.
- `scripts/journal.sh` — append-only per-event lifecycle journal writer:
  one JSON line `{ts,event,actor,action,outcome,detail,log,host,pid}` to
  `state/<EVENT_ID>/journal.jsonl`. ALWAYS exits 0 (a journaling failure must
  never kill a live event). Subprocess-invoked (the notify.sh pattern).
- `scripts/assert-ready.sh` — the green gate: exit 0 only when
  `state/<E>/preflight-result.json` is PASS + config-sha-matched + ≤24 h AND
  `configs/.readiness/<basename>.json` is hash-matched with no unacked
  `confirmed:0` keyword. `rehearsal-*` basenames exempt. Pure read.
- `scripts/descope-event.sh` — resolves the exact market-event ticker across
  schema-3 RunPlans, verifies the complete selection, refuses duplicate active
  attempts or any launched/other-terminal member, and records typed `descope`
  on every active lane before durably publishing `state/<E>/DESCOPED.json` and
  paging. The legacy-only projection remains available only when no exact or
  related typed RunPlan exists; it can never hide a schedulable typed run.
- `scripts/watcher-liveness.sh` — page-only "nothing watches the watchers"
  check for due-but-dead arm-on-url watchers.
- `scripts/install-t0-agents.sh` — the internal macOS adapter for exactly one
  schema-3 webcast RunPlan. It requires the matching AccessPlan, installs one
  `arm-on-url.sh --run-dir ...` launchd job, reads the loaded job back, and
  delegates the durable `scheduled` fact to `schedule-proof.py`. It refuses a
  placeholder/past fire, host mismatch, argv mismatch, or partially installed
  descriptor; an on-disk plist is not coverage. The plist environment pins
  `PATH=/opt/homebrew/bin:...` (launchd's default resolves python3 to Apple's
  3.9 — no tomllib/playwright — and the fire path dies; AMAT 2026-08-13), and
  the launchctl readback accepts both key renderings (Darwin 25 quotes
  descriptor keys; the bare-only regex rolled back a correctly loaded job).
  `--selftest-readback` pins both fixtures offline.
- `scripts/study-nightly.sh` — RunPlan-driven variant-study runner in call
  order (generalizes the hand-written per-day study scripts). A UTC date
  selects only immutable schema-3 plans by `starts_at_utc`; each plan pins the
  exact config and event env. Terminal/archived attempts are closed evidence,
  not candidates: the typed reducer filters them before collision detection,
  so a superseded predecessor never collides with its active successor.
  Issuer/date filename tokens never select an
  event, and the script prevalidates the complete set before measuring
  anything. Parsed config/env staging metadata is checked independently, so
  an expected event whose RunPlan compilation failed makes the date nonzero
  rather than disappearing behind its ready siblings.

  The RunPlan's config hash is an immutability boundary. When autopsy says a
  dead word needs wider spelling variants, `variant-widen.py` runs in proposal
  mode only. `study-nightly.sh` never passes `--write`, never changes the
  pinned config, never remeasures a hypothetical edit, and never auto-acks that
  unmeasured result. It leaves a reviewable proposal in `/tmp`, raises the
  stable critical `study_failed` / `nightly-study` AlertRequest while the plan
  still verifies, and makes the date nonzero. Applying that proposal is a
  deliberate config change which must produce a new config hash and new
  RunPlan attempt before another study.

  Measured dead words for which widening is not the remedy may still be
  acknowledged as knowingly unserved under the existing 1¢ non-crossing
  posture. More than one third of the board dead is refused as a broken
  config, and a phrase-screen mismatch remains a descope decision rather than
  a detectability acknowledgement. The script produces/resolves readiness
  only; it never calls `preflight-event.sh`. The next `prepare-day` stage,
  `preflight-nightly.sh`, is the sole producer of proof-bearing
  `study_completed` and preflight verdict receipts. `--dry-run` may use
  temporary selection files but makes no repository, lifecycle, journal, ack,
  config, or notification change.
- `scripts/green-guardian.sh` — re-checks the green gate shortly before the
  watcher fires and restores the snapshotted green SET if an interrupted
  night-before edit left the event red. Makes the safe state the default state
  rather than something a live session must remember to restore.
- `scripts/night-before-sweep.sh` — read-only "will this slate actually fire
  tomorrow" sweep: green gate, the **loaded** launchd descriptor being in the
  future (not merely listed), the loaded argv's posture flags, the pinned
  binary, and stale `$HOME/<slug>-stream.url` files.
- `scripts/schedule-nightly.sh` — the scheduling join behind `opsctl
  prepare-day`. It enumerates immutable RunPlans, refuses a zero-plan date, and
  delegates only `preflight_passed` attempts to their host-appropriate
  proof-bearing installer. Phone retains `box-event-install.sh`. Webcast
  dispatch compares the plan's `execution_host_id` to
  `EARSHOT_LOCAL_EXECUTION_HOST` and `EARSHOT_BOX_EXECUTION_HOST` (default
  `earshot-useast1`): local goes to the byte-identical `install-t0-agents.sh`
  argv, box goes to `box-webcast-install.sh`, and any unknown or ambiguous host
  refuses before either installer. The facade consumes `preflight-nightly.sh`
  through `control.preflight`'s public script boundary; it does not own that
  gate. A failure of one verified plan raises one stable run-bound critical
  alert and does not stop later siblings. A corrupt/no-plan failure has no
  trustworthy ledger and therefore uses the legacy date-level notifier. Alert
  delivery failure never changes the original nonzero scheduling result, and
  dry-run invokes neither installers nor notification adapters.

  A `preresolved` box webcast still needs the laptop-produced URL file pushed
  into the box attempt's freshness window. Until the S5 resolve-push launchd
  mode exists, this is an explicit operator step through lane R's
  `scripts/push-url-file.sh`; this node does not reproduce or edit that
  transport. The systemd timer remains T-75 and waits through the AccessPlan's
  resolver leg, but scheduling the URL push itself remains open boundary work.

## Does not own

- The launchers themselves (`control.launch`) — this node's gate is CALLED
  from them, additively; dial/redial/watchdog logic is untouched.
- `scripts/preflight-event.sh` and the verdict artifact it writes
  (`control.preflight` owns the gates; this node only consumes
  `preflight-result.json`).
- `configs/.readiness/**` (generated evidence, `evidence.benchmark` method)
  and `scripts/variant-study.sh` (its writer).
- `scripts/notify.sh` (`platform.notify`), `scripts/slate-stage.sh` /
  `scripts/slate-time-merge.py` (`control.prepare`), grading
  (`control.grade`).
- `state/**` content itself — journals are runtime evidence, ungoverned by
  design; this node owns the writer and the conventions, not the bytes.

## Input contracts

None registered yet. The de-facto inputs are files: staged configs and
`configs/*-autorun.env` (control.prepare — `eventctl.py init`/`init-date`
parse `[event].id`, `[event].starts_at`, `[[sources]].kind` via `tomllib`
and `CONFIG_PATH`/`EVENT_DATE`/`EARSHOT_ACCOUNT_PROFILE` via
literal-assignment regex, never filename shape or shell evaluation), the
external account profile named by that locator,
`state/<E>/preflight-result.json` (written by
control.preflight since 2026-08-04), and
`configs/.readiness/<basename>.json` (evidence.benchmark). The remaining
de-facto inputs become typed doors in later Phase D slices
(`preflight-result.json` is already shaped for it).

## Output contracts

- `control.run.plan` — the immutable run plan,
  `var/runs/<EVENT_ID>/<RUN_ID>/control/run-plan.json`. One per event
  attempt: schema version, event/run ids, UTC creation and start
  timestamps, ingress lane, stable execution-host ID, Kalshi environment,
  market-event ticker, stable account fingerprint, explicit execution posture,
  repo-relative config/env paths
  with SHA-256 hashes, the lane's required preparation receipts, the
  run-relative artifact destinations (receipts, status, all six audio-evidence
  constructor paths, book tape, order-evidence journal, independent raw FIX
  observation tape, runtime report, grade), and its own canonical
  `plan_sha256` (computed with that field omitted). The six audio keys are
  `audio_raw`, `audio_frames`, `audio_manifest`, `audio_commit`, `audio_lock`,
  and `audio_wav_dir`; the FIX key is `fix_observation_tape`. Repeated
  identical `init` is idempotent; a divergent or unsupported-schema plan at
  the same path is refused read-only, never rewritten or migrated. For an
  execution-enabled plan, host/account identity comes from either a complete
  explicit override pair or schema-1 `execution-identity` JSON returned by
  `execution-profile.sh resolve` after it rechecks the selected signer.
- `control.run.receipts` — the append-only lifecycle ledger,
  `var/runs/<EVENT_ID>/<RUN_ID>/control/receipts.jsonl`. Exclusive
  `flock` + `O_APPEND`, one canonical JSON line per write; every receipt
  carries `sequence`, `previous_sha256` (chain anchored at `plan_sha256`),
  and its own `receipt_sha256`. Typed transitions re-verify the whole
  chain plus the current config/env hashes before appending; legacy
  `compat` receipts are observations that can never advance or terminate
  typed state. `control/status.json` is a DERIVED snapshot
  (`"derived": true`, `"authority": "control/receipts.jsonl"`) and is
  never authoritative.

Across hosts, the same two doors retain one authority chain. Before launch the
laptop may hold the preflight prefix. Dispatch publishes that exact tree to the
RunPlan's execution host; the execution host then owns every scheduler/runtime
append. Retrieval accepts the laptop prefix and imports the box extension
without synthesizing receipts. A later local `evidence_pulled`/`graded` tail is
the normal inverse relation (remote ledger is then a prefix of local). A
laptop-hosted terminal run instead records `evidence_consolidated` after the
offline audit validates its in-place artifacts; `evidence_pulled` is the
stronger remote-transfer form of that same grading prerequisite.

**Invariant (E11): a run's chain may be extended by exactly one host at a
time — the host that currently holds its ledger authority.** Authority
transfers to the execution host at dispatch (both installers call
`eventctl authority --transfer-to` after the box has verified and published
the prefix; a CLOSED attempt is not transferred, its `archived` tail is
laptop bookkeeping) and returns at pull (`pull-box-state.sh` calls
`--returned-from` right after each successful publish/advance, before it
records `evidence_pulled`). Between the two, `Ledger.append` refuses on this
copy: appending would fork the chain permanently, because the box is minting
receipts at the same indexes. Order of operations, always: schedule → pull →
close → dispatch. Residual: after a `--prelaunch` pull the returned authority
is a snapshot claim — if the box's timer is still armed the box may extend
again; re-pull before closing if time has passed (the full fix is live
read-through, M4).

`state/<E>/journal.jsonl` and `state/<E>/DESCOPED.json` remain the
de-facto legacy outputs, consumed by the missed-arm reconciliation
(control.grade) and the day-after reconstruction; `journal.sh` bridges
each legacy line into the active run's ledger as a `compat` receipt.

## Side effects and exclusive resources

- Appends to `state/<E>/journal.jsonl` (O_APPEND single-line writes; safe
  under concurrent actors).
- Writes under `var/runs/<EVENT_ID>/<RUN_ID>/` (gitignored): the plan via
  a uniquely named temp file + file fsync + rename + parent-directory fsync,
  receipts via exclusive `flock` on the receipts fd itself + `O_APPEND` + one
  write + fsync, and the status snapshot through the same durable temp/rename
  publication under the receipts lock; the host-private authority sidecar
  `control/authority.json` through the same temp/rename publication under the
  receipts lock. Readers take a shared flock. Nothing
  else may write those four files.
- `execution-profile.sh configure` is the sole deliberate external write in
  this slice: when the operator explicitly names an account env, it atomically
  replaces that one file while preserving its restrictive mode and all bytes
  outside its marked block. It refuses symlinks and any group/other-readable
  credential file. Repository verification never invokes it on a real account
  env.
- The internal installer writes one RunPlan-bound launchd plist and bootstraps
  it into `gui/<uid>`. Only the loaded-job readback plus schedule proof may
  advance `scheduled`.
- Delegates verified RunPlan-bound study/schedule failures to
  `scripts/alertctl.py`, which owns durable request, delivery, retry, and
  acknowledgement facts. Legacy `notify.sh` remains only where this node has
  no trustworthy RunPlan ledger (plus older descope/gate/watcher callers not
  migrated by this slice).
- Never places calls, never sends orders, never touches Twilio/Kalshi APIs.

## Failure contract

- `journal.sh` exits 0 unconditionally; callers append `|| true`.
- `assert-ready.sh` refusals are single-line `REFUSED: <why>` on stdout,
  exit 1; launchers page and exit 7 on refusal; `EARSHOT_GREEN_OVERRIDE`
  proceeds but pages ("an override that pages is a decision, not a bypass").
- `install-t0-agents.sh` refuses/rolls back when bootstrap or loaded-job proof
  fails; a partially installed job must never gain a `scheduled` receipt.
- `watcher-liveness.sh` pages the missing-slug diff and exits 1.
- `descope-event.sh` refuses without a reason, on a lane event ID rather than
  the exact market-event ticker, on an invalid/corrupt related RunPlan, on
  multiple active attempts for one lane event, or if any selected run has
  launched or terminated as anything other than `descope`. It prevalidates the
  whole set and publishes no legacy projection until every selected ledger
  durably contains `descope`; a stopped append is loud and idempotently
  recoverable by rerun.
- `eventctl.py` exit codes: 0 success (including idempotent no-ops and a
  `compat` with no active run), 1 refusal or integrity failure, 2 usage,
  3 recoverable truncated tail. An illegal transition modifies nothing; a
  refused `init` writes nothing. `verify` fails loud on any mutation of
  the plan, any earlier receipt, any newline-terminated
  corrupt receipt (including the last one), or any corrupt/missing sibling run
  candidate. Pinned config/env drift fails loud on ACTIVE runs only: a
  terminal/archived run legitimately outlives its pinned bytes (its successor
  owns the current ones) and verifies OK with a labeled
  `pinned inputs superseded` note — chain integrity stays hard for closed
  runs. **A GRADED run is also exempt (2026-08-18), but conditionally.**
  `TERMINALS` names only failure transitions and `closed` requires `archived`,
  so a SUCCESSFUL run stays `active` forever and its verification kept
  re-reading mutable worktree bytes — removing `max_event_notional_cents` from
  every config (operator ruling **O8**) retroactively refused all four
  already-graded 26AUG18 runs. `init` now banks the pinned bytes as
  `control/pinned-config.toml`, and a graded run softens to
  `pinned inputs superseded (run is graded)` **only while that snapshot still
  hashes to `plan.config_sha256`**; with no snapshot the bytes are genuinely
  gone and the refusal stands. The snapshot is self-proving — accepted only on
  a hash match, so a forged one is indistinguishable from a corrupt one and
  both are refused — and `_check_pinned_inputs` deliberately does NOT consult
  it, keeping the active-run tripwire hard. Symmetrically, `record` exempts
  closing transitions (`descope|blocked|aborted|failed|archived`), the
  post-hoc `graded` verdict, and the post-terminal audit tail from the
  pinned-input check: closing or grading a run must never require restoring
  old bytes, work-advancing transitions (`prepared`, `launched`, …) stay hard,
  and the exemption cannot legalize an illegal transition (the typed gate
  still runs). `init-date` terminal-closes
  never-launched predecessor attempts whose pinned config drifted
  (`blocked`+`archived`, actor `stage`, idempotent); a LAUNCHED predecessor is
  an explicit refusal a human resolves, and aborted/failed predecessors keep
  their post-mortem tail open. Only a non-newline-terminated final JSONL
  fragment is diagnosed
  as recoverable (repair = manual truncation to the reported byte offset;
  eventctl never auto-truncates the ledger).
  Only the current RunPlan schema is writable/readable. Existing schema-1 and
  schema-2 attempt directories are immutable historical evidence: a colliding
  `init` checks them before any `mkdir`/`O_CREAT` and fails without changing
  even their directory set. The schema component in a default RUN_ID prevents
  accidental collision while leaving those directories byte-for-byte intact.
  The `journal.sh` compat bridge is wrapped so an eventctl failure can
  never break journal.sh's unconditional exit 0.
- Ledger authority (E11): while `control/authority.json` says `remote`, every
  write through `Ledger.append` — `record` (any outcome), `compat`, and
  `init-date`'s supersede-close — is a one-line `REFUSED: E11 …` naming the
  pull (`pull-box-state.sh --prelaunch --event <E>`, or `--date` after a
  terminal run) and the `--reclaim` escape; nothing is modified. Idempotent
  no-ops still exit 0 (the check runs after the gate has said "append"), so
  `init-date`/`prepare-day` re-runs stay green; a dispatched attempt whose
  pinned config drifted is a loud `supersede-close failed: E11 …` refusal,
  not a fork. `authority` refuses a host other than the plan's
  `execution_host_id`, an invalid `--by`, and `--reclaim` without `--reason`;
  same-mode moves are no-ops. A sidecar that does not parse or belongs to
  another plan fails writers closed (`FAILED: authority sidecar …`) while
  `verify` and `status`/`--show` stay tolerant (`write_authority: invalid`).
- Execution identity is all-or-nothing. An enabled `init` accepts both
  explicit recovery overrides (`--host-id` and `--account-fingerprint`) or
  resolves both from the event env's literal portable
  `EARSHOT_ACCOUNT_PROFILE` (literal `ACCOUNT_ENV` is the compatibility
  fallback); it never mixes the two. Missing/computed/nonportable locators,
  stale keys, config-vs-ambient signer mismatches, malformed helper output,
  and missing pinned binaries refuse before a run directory is created.
  `init-date` resolves each event independently, so concurrent events may pin
  different accounts with no per-event fingerprint patch. Neither profile
  command prints credential values or private-key bytes.
- `run-host-boundary.py` exits 0 only for an atomic publication, a verified
  prefix advance, or an already-compatible no-op. Exit 1 leaves the
  authoritative receipt prefix unchanged. A crash before the receipt append
  may leave only source-verified evidence files beside the old valid prefix;
  rerunning completes the same reconciliation. The helper requires an exact
  `--expected-host` match and a canonical destination under
  `var/runs/<EVENT_ID>/<RUN_ID>/`.
- `opsctl prepare-day` never converts a partial date into an all-clear. It
  returns 1 if stage, access, study, preflight, or schedule reports any problem while
  still allowing those aggregate scripts to process ready siblings.
- `study-nightly.sh` fails before its first detector invocation if no schema-3
  RunPlan exists for the requested UTC date, if a selected plan or pinned
  input fails `eventctl verify`, if two ACTIVE plans ambiguously select one
  event or config (closed attempts are filtered by the typed reducer first,
  and a config whose only attempts are closed is named as such — re-stage the
  date), or if parsed staging metadata expects a config that has no plan.
  Filename dates are never a compatibility fallback. Once verified, pinned
  config bytes are read-only: a widening need creates a proposal plus a
  critical alert/nonzero verdict and requires a new RunPlan attempt. Study
  never invokes preflight.
- `schedule-nightly.sh` continues siblings after a verified run's failure and
  raises one critical AlertRequest with a stable failure-class code/dedupe key.
  It calls `alertctl` once and inherits its bounded delivery policy; it does
  not loop or blind-redial. Failures without a trustworthy plan use one legacy
  date-level notification. Any alert-adapter failure is logged but the
  scheduler's original nonzero result is preserved. Webcast host selection is
  exact: an unset local identity is allowed only for a plan matching the
  configured/default box identity; every other host is a run-bound
  `schedule_host_unsupported` refusal with no installer invocation.

## Parallel work boundary

Safe alongside every other node: all writes are additive (journal appends,
new plists, new DESCOPED records). Do not edit launcher gate blocks and
`assert-ready.sh` semantics in separate parallel claims — the gate contract
spans both sides and must move in one change.

## Verification

- `bash -n` on every owned script.
- Run ledger: `python3 -m py_compile scripts/eventctl.py
  tests/eventctl_lifecycle.py`; `python3 tests/eventctl_lifecycle.py`
  (isolated temp repos — replay, idempotency, join gates, mutation
  detection, truncated tail, cross-process concurrency, init-date
  selection/ambiguity, and `TestAuthority`: transfer refuses record/compat
  with no row and one-line E11 stderr, idempotent no-ops still pass, return
  and reclaim, foreign host refused, corrupt sidecar fails writers closed
  while verify/status tolerate, `init-date` refuses to supersede-close a
  dispatched run, plan/artifacts untouched — each proved red by disabling
  the guard); `git check-ignore
  var/runs/TEST/RUN/control/run-plan.json`; `cargo test --test
  architecture_contract`; `scripts/doc-lint.sh`.
- Host boundary: `python3 -m py_compile scripts/run-host-boundary.py
  tests/run_host_boundary.py`; `python3 tests/run_host_boundary.py` (incl.
  `test_authority_sidecar_never_crosses_the_boundary`, red with the
  `HOST_PRIVATE` filter removed). The suite
  uses temporary local/remote repositories and invokes no ssh, rsync,
  systemctl, launchctl, dial, or market API.
- Operations facade, RunPlan-only study date selection, pinned-config
  immutability, study/preflight separation, and nightly alert boundaries:
  `python3 tests/opsctl.py`; schedule success/failure/sibling/dry-run drill:
  `scripts/schedule-nightly.sh --check` (local webcast, box webcast, unchanged
  phone route, sibling continuation, dry-run non-mutation, and unknown-host
  refusal — every assertion is `[[ … ]] || fail $LINENO` / `must_not`, because
  macOS bash 3.2 does not trip `set -e` on a bare `[[ ]]`; measured
  2026-08-17 that this gate printed PASS with its E12 assertion made
  impossible, SYSTEM-STATE E14); public-surface governance:
  `python3 tests/ops_surface.py` and `scripts/doc-lint.sh`.
- Execution identity: `bash -n scripts/execution-profile.sh
  tests/execution_profile.sh`; `tests/execution_profile.sh`; `cargo test --lib
  auth`; `cargo test --bin earshot
  local_account_identity_uses_the_same_config_first_selector_as_arm`.
- Journal round-trip: `scripts/journal.sh --event JOURNAL-SELFTEST --actor t
  --action note --outcome ok --detail 'x "y" <z>'` then `python3 -c
  "import json;[json.loads(l) for l in open('state/JOURNAL-SELFTEST/journal.jsonl')]"`.
- Gate both-ways: `assert-ready.sh` against a staged config REFUSES before
  its preflight and passes after a PASS verdict; `rehearsal-*` passes
  unconditionally.
- The launcher integration is covered by `tests/launch_event_posture.rs` /
  `tests/launch_webcast_posture.rs` (green with the gate blocks in place,
  2026-08-04).
- `tests/typed_scheduler_boundary.sh` is the offline scheduler/fire-time drill:
  loaded-job rollback/readback, endpoint-bound PCM proof, crash-gap recovery,
  exactly one launch, and launcher-PID-only teardown.

## Migration status

New node (2026-08-04, prep-night for the 10-event 2026-08-05 slate). Journal
insertions cover the tonight-critical actors (launch-event, launch-webcast,
arm-on-url, run-arm, event-autorun, redial-supervisor, preflight-event);
remaining actors (slate-stage, variant-study, webcast-autorun,
install-event-timer, run-verdict) get theirs in the week's follow-up.

Phase D reliability integration is implemented and offline-tested: the run
identity and lifecycle ledger are live (`control.run.plan` /
`control.run.receipts`), `journal.sh`
bridges legacy lines into the active run as `compat` receipts,
`event-status.sh` verifies and prefers typed status, enumerates the union of
RunPlans and slate entries, and returns nonzero with
`STALE_PREFIX_POSSIBLE` when a scheduled active run's execution-host tail has
not been pulled. It never hides typed corruption behind legacy state. The
`stage-nightly.sh` compiles run plans nightly via `eventctl.py init-date`.
Account/host identity is a stable per-account prerequisite rather than a
per-event patch: provision an account env once, then every event naming its
portable profile locator is checked against the pinned binary during nightly
plan compilation and again against the actual signer at arm time.
The runtime now coordinates report, decision, order, audio, book, and raw FIX
evidence close; launchers consume only schema-3 plans; the scheduler proves the
loaded job and fire-time capture boundary; and the deterministic grader
consumes the run ledger, including an independent FIX-tape replay and the
derived date audit. Run-bound failures now carry durable bounded delivery and
explicit acknowledgement facts. Still open are production
rehearsal/deployment, transcript-backed precision, and remote status
read-through before terminal evidence pull.
