# Node: `control.grade`

## Job

Turn a terminal run's artifacts into an explicit operational and detection
quality verdict without changing future posture.

## Owns

Operational PASS/WARN/FAIL rules in `scripts/run-verdict.sh`, log/transcript
comparison in `scripts/validate-from-log.py`, the missed-arm reconciliation
in `scripts/missed-arm-check.py`, and the always-speaks post-event verdict mail
in `scripts/post-event-report.sh`. `scripts/pull-box-state.sh` owns the
read-only box-to-grader evidence transfer; it does not supervise the remote
run.

**The post-event report exists to make silence unambiguous.** Unattended runs
page only on failure paths, so no email reads identically as "it worked" and as
"the watcher never fired". This job always mails, from `state/` alone — no live
session, no interactive agent — and always exits 0, because a reporting job
that can fail closed would reintroduce the silence it was built to remove.

**Evidence is per LANE, and a box-hosted lane leaves nothing on the laptop.**
`scripts/pull-box-state.sh --date YYYY-MM-DD` copies both evidence authorities:
legacy `state/<EVENT_ID>/` lands under `state/<EVENT_ID>/box/` (the provenance
layout `scripts/event-status.sh` already reads), while typed
`var/runs/<EVENT_ID>/<RUN_ID>/` lands at that same canonical run path locally.
The typed path does not need a `box/` namespace: its immutable RunPlan pins the
execution host, event, run, config, environment, and account identity, and the
pull receipt names the source. `scripts/deploy-box.sh` excludes these runtime
artifacts, so this explicit read-only pull remains mandatory before grading.

The transfer is a publish operation, not an in-place sync. Every remote tree is
first rsynced to a private local staging directory. A second checksum dry-run
must report no delta, symlinks are refused, and each staged typed run must pass
`eventctl.py verify --run-dir` before any canonical destination is created.
All conflicts are check-only validated before publication. An existing local
legacy tree must match exactly (apart from the two deliberately excluded
reproducible products, `earshot-pinned` and `rules`). Typed reconciliation goes
through `control.operate`'s network-free `run-host-boundary.py`: the normal
topology is a laptop preflight prefix extended by the box, while a repeated
pull after local `evidence_pulled` is the inverse prefix relation. Neither
direction is treated as divergence; anything that is not an exact receipt
prefix, or any unexplained/changed immutable evidence, fails loud before the
authoritative receipt file is modified.

After publication, every local typed run is verified again through the public
eventctl door. Only runs that reached `completed`, `aborted`, or `failed` are
published and receive the append-only `evidence_pulled` transition.
Active/pre-launch runs are inventoried, checksum-staged, and verified but are
not published into the laptop's canonical run root: a growing live tape must
not create a second partial authority. A legacy-only state copy can never
manufacture a typed receipt. Repeating a terminal pull is idempotent: the
remote receipt file is the prefix and the already-recorded local tail is
preserved.

`--prelaunch` is the one opt-in exception, and it is bounded at `launched`, not
at "active". A run that has not reached `launched` has no `capture/`, `book/` or
`orders/` content at all — only atomically published `control/` proofs and the
append-only ledger — so the growing-tape hazard the default refusal names cannot
exist yet. Once `launched` is reached the run is refused again, `--prelaunch` or
not. It publishes through the same `run-host-boundary.py` prefix primitive as
every other direction and records **no** receipt: `evidence_pulled` requires a
terminal execution outcome, and returning a preflight prefix is not one. The
returned receipts are their own proof.

This exists because seam S-C mints `preflight_passed` on the box while
`schedule-nightly.sh` and `box-webcast-install.sh` both read that receipt from
the laptop ledger, and a box-only terminal receipt fails local `verify`
outright. The 2026-08-14 box rehearsal hit all three symptoms of that one
missing door and had to move run directories by hand. The supported command is:

```sh
scripts/pull-box-state.sh --event <EVENT_ID> --prelaunch   # after box preflight
```

Never close that gap by re-recording the receipts locally: a hand-written
`preflight_passed` is a fabricated proof of a build that happened on another
host, which is the exact class the typed ledger exists to prevent. The opposite
direction — getting a RunPlan TO the box before its on-box preflight — is
`box-webcast-install.sh --dispatch-only` (`control.supervise`), not this script.

`--supersede-legacy` addresses the legacy `state/<EVENT_ID>/box/` tree, which
has no typed identity and so is compared byte-for-byte. Re-running preflight on
the box legitimately rewrites `detector-smoke.log`, `preflight-build.log` and
`preflight-result.json`, which makes the next pull of that event refuse
wholesale. The refusal stays — merging divergent evidence in place is the E3
rule — but the flag turns the operator's hand-`mv` into a first-class step:
the local tree is archived to `state/<EVENT_ID>/box.superseded-<UTC>`, the
diverging filename is printed, and the box copy is then published. It never
overwrites and never deletes, so superseded evidence stays readable; with
nothing diverged it is a no-op. Without the flag the refusal now names it
rather than dead-ending. Like every pull argument, `--check` refuses it.

HIMS 26AUG10 needed all three defects at once to read clean. Its phone lane put
**2 orders on the wire and spent 194¢**, and the day was reported as
`1 staged, 1 captured, 0 orders … 1¢ non-crossing posture` off the laptop's
webcast twin. The three:

1. `staged_for` globbed `phone-*-<date>-arm.toml`, but `phone-lane-derive.py`
   writes `<issuer>-<date>-phone-arm.toml` — the pattern matched **nothing**, so
   the phone lane was never in the denominator.
2. `out[issuer] = {…}` overwrote, so a two-lane issuer kept one lane.
3. Every artifact glob was one level deep (`state/*/report.json`), so a pulled
   box run under `state/<E>/box/` was invisible.

Now: lanes are kept as a list, but only a lane bound to an exact typed RunPlan
config or carrying an unambiguous exact-event report/journal may supply armed
posture. If multiple configs share one legacy event ID, the event artifact
selects none of them; an exact RunPlan `config_path` is required.
Never-armed configs remain visible commitments and cannot turn a fillable miss
into a 1¢ observe. `fill_posture` is **any artifact-bound armed lane's**; health
is the **best armed lane** and names it; `_artifact()` prefers `box/` and returns at most one path per
event dir so nothing double-counts; and `-SWEEP` dirs are excluded outright —
`sweep-hft` is a separate project sharing only the Kalshi account, and its fills
must never inflate this system's numerator.

An issuer that reached the wire produces no finding by design, so the tool now
prints an explicit `✓ ON THE WIRE: <issuer> — N order(s) with send_ts` line.
Without it a fully successful day and a day where nothing was staged print the
same thing.

**The denominator is the day's STAGED CONFIGS, not the surviving captures.**
Until 2026-08-07 reconciliation iterated over captures, so an event that never
armed had no row to be missing from — it was invisible, not flagged. On
2026-08-06 ABNB and LYFT both died on a venue registration wall and the tool
reported **0 MISSED-ARM** for a day that missed two outright and armed a third
into a dead stream. A grader whose denominator is its own successes cannot
report a total failure. Staging is the commitment, so it is the population. It
is not proof that the lane armed: summaries report armed/committed lane counts
separately.

Silence now resolves to one of six verdicts rather than two, because "placed
nothing" has several causes and only some are benign:

| verdict | meaning |
|---|---|
| `MISSED-ARM` | staged, board traded, nothing on disk at all |
| `ARMED-DIED` | armed, then lost every leg — it could not hear the call |
| `ARMED-NO-FILL` | a crossable ask existed and nothing reached the wire |
| `ARMED-NO-RECORD` | armed fillable, left no `report.json` — unknowable from disk |
| `ARMED-OBSERVING` | every intent was priced above its cap, or the posture is 1¢ |
| `DESCOPED` | a recorded decision, not a failure |

Three rules keep those honest. **Posture is read from the config**, never
assumed — the old code justified an observing verdict with "1¢ non-crossing
posture" wording for events armed at a 97¢ fill cap, citing a posture the
config did not have. **A run that died did not observe**: job liveness is not
run liveness, and AAOI-26AUG06 armed twice, lost its stream within 32 s both
times, and was graded benign. **Unknown is never reported as fine**:
MELI-26AUG05 filled at 90¢ and its record died with the run, so it grades
`ARMED-NO-RECORD` rather than passing silently.

And two guard against crying wolf, which is how a grader gets ignored. A
descope is honoured in **either** form — `DESCOPED.json` or a journal
`action=descope` line — and matched on the issuer+date prefix, because
`descope-event.sh` keys EBAY's record to the bare event ticker while the
webcast twin is `…-WEBCAST`; an exact-path lookup reported both of
2026-08-05's legitimate descopes as misses. And an intent that found no ask at
or under its cap is a **correct decline**, not a defect: TLN-26AUG05's one
intent saw a 99¢ ask against a 97¢ cap, exactly where `ev_gate` refuses.

**Missed-arm reconciliation grades the run that never happened.** Its question
is one line — *issuer X's board traded, we placed no orders* — and it is
deliberately INDEPENDENT of which upstream node broke.
It fires whether `control.discover` lost the event, `control.qualify` lost the
bridge, or the wrong tool was pointed at the call, because it reads only the
evidence on disk. An execution verdict on a run that was never launched is
still an execution verdict, which is why it lives in this node.

It keys on a **traded** board at any point in the event's bounded UTC day, not
an `open now` sample. The checker reads public one-hour event candlesticks and
sums every market's interval volume; a positive earlier interval remains
activity even when the last interval is zero. A lookup failure or malformed
history is `UNDETERMINED`, never clean. This matters because SPOT was open and
repricing at 12:02Z and closed by 15:00Z, while ETOR's staging-time no-open
sample preceded 26,496 contracts of later activity. Two cry-wolf guards are load-bearing: legs under 60 s are not
coverage (PGR's legs 07–23 held ~12.3 s each after the call ended), and
`state/` run dirs are date-filtered by event-ticker suffix (an unfiltered scan
reported GEV's 2026-07-22 event as a miss on 2026-08-04).

Existed-nowhere on 2026-08-04: Spotify and Progressive both produced zero
orders on traded boards and the system said nothing, so the question had to be
asked by a human hours later.

**The scheduled leg** (`scripts/missed-arm-daily.sh` +
`configs/launchd/com.earshot.missed-arm.plist`, added 2026-08-04): launchd
fires the wrapper daily at 17:30 local (23:30Z) — after the last US afternoon
calls end, before the UTC date rolls, so `date -u +%F` still names the event
day being reconciled. The checker writes and alerts nothing by design; the
wrapper raises a standalone proof-bearing critical alert on any non-zero exit
— MISSED-ARM (1) and UNDETERMINED (≥2) both require the phone channel with a
deterministic date/code identity; a clean 0 only logs. Because a genuine miss
may have no RunPlan, this ledger lives under `state/alerts`, never `var/runs/`.
After that checker returns, the same scheduled wrapper always runs
`audit-day.py` for the identical UTC date. Its failure raises the separate
deterministic `audit_day_failed` critical alert; a missed-arm failure cannot
skip or mask the typed index result.

**Box pull gate.** Events launched on the us-east-1 box are not gradeable from
the laptop until `pull-box-state.sh --date <D>` succeeds. Do not replace it with
an ad-hoc rsync: that loses typed run evidence, immutable identity checks, and
the `evidence_pulled` lifecycle receipt. A scheduled grade that fires before a
successful pull is expected-incomplete and must not be treated as authoritative.

**Schema-3 run audit.** `scripts/run-audit.py --run-dir <dir>` is the one
proof-bearing grader for an immutable RunPlan attempt. It is offline and first
calls the public `eventctl.py verify` door. It then verifies the finalized
runtime report, independently replays the version-4 order-evidence envelope and
transition reducer, verifies the protocol-2 raw book tape and byte-bound commit,
checks the audio manifest/commit, raw/sidecar fingerprints, and derived WAV
fingerprints, and derives the configured execution transport from the pinned
config bytes. A FIX run additionally receives an independent protocol-1
observation-tape replay: exact JSON shape, immutable identity and endpoint,
tape/source sequencing, cross-process hash chain, raw FIX framing and typed
projection, duplicate pointers, session-close accounting, and every
create-new close receipt's exact tape prefix are all verified. A REST run must
instead report explicit `not_applicable` evidence with both FIX health fields
null and must have no stray FIX tape or commit claim. Stored absolute paths are
treated only as remote provenance: each must end in its exact RunPlan artifact
destination, while bytes are read only from the local pulled run directory.
Thus a box-produced receipt stays verifiable after byte-preserving retrieval
without trusting a remote path on the laptop.

Order evidence is the sole accounting authority. The runtime report must name
the same intents, attempts, exchange identities, acceptance/fill state, and
unresolved client IDs; disagreement is an integrity failure, not a choice of
which counter looks more plausible. Every detection and every order transition
is retained in the grade. The FIX tape fingerprint, replay summary, and
per-session commit fingerprints are retained in the same immutable grade;
REST grades retain an explicit `not_applicable` FIX summary. Audio WAV paths
are emitted for later transcript scoring. Before audit,
`score-precision.py --run-dir` can publish the separate immutable
`grade/precision-score.json`; detection precision remains `not_resolvable`
until the EVIDENCE-owned auditor verifies and joins that score.

Book analysis uses integer cents and centi-contract quantities and has exactly
four classifications: `no_snapshot`, `empty_at_detection`,
`depth_available_no_exhaustion`, and `depth_exhausted_after_detection`. The
measured event is named `cap_depth_exhaustion`. Its timestamp is the exact book
frame receive monotonic timestamp, so detection-to-exhaustion and
wire-to-exhaustion are emitted only on the same host monotonic clock. It is
never called a competitor trade or a sweep: a disappearing level does not
establish that causality.

## Does not own

It does not supervise/relaunch the run, capture missing evidence after the
fact, promote a keyword, edit thresholds/configs, update the corpus, or deliver
an alert channel itself.

## Input contracts

Operational grading consumes a terminal `report.json` and per-leg data.
Detection validation consumes the arm log, exact event config, public
transcript, and optional aligned word timestamps. The precision scorer derives
the recorder-to-detector offset from a global cross-keyword hypothesis search;
it does not assume the two processes began within a short fixed window. More
than one timestamped intent is required to establish an automatic offset, and
equally supported disjoint offsets are refused as ambiguous. The box
pull consumes the remote legacy event tree plus each immutable
`control.run.plan` / append-only `control.run.receipts` tree selected by date or
exact event identity.
Typed precision first verifies the public `eventctl` door, schema-3 RunPlan,
exact canonical run identity, pinned config bytes/hash, and runtime report
identity/hash. Truth input is bound by SHA-256; audio-derived truth also binds
the generated transcript and model. Only timestamped truth can produce a
numeric precision score.
Missed-arm activity consumes public event candlesticks at a one-hour interval
over `[event-day 00:00Z, next-day 00:00Z)`. Staged configs remain the
commitment denominator; exact RunPlan config binding or an unambiguous
exact-event report/journal is the separate armed-lane fact.

## Output contracts

`run-verdict.sh` emits PASS, WARN, or FAIL with stable exit semantics: absent
report or zero audio is failure; surviving audio with lost legs is warning.
Terminal no-report, zero-audio, and unparseable verdict failures raise a
standalone proof-bearing critical alert keyed by event/run location and report
fingerprint. A surviving-leg warning retains informational local notification.
Validation emits per-keyword counts/timing evidence. The target is one
versioned `RunGrade` JSON retaining both provenance and uncertainty.

The typed scorer publishes schema-1 `precision-score` at the fixed
`grade/precision-score.json` path with RunPlan/event/run identity, config and
runtime-report fingerprints, truth fingerprint, deterministic per-intent
verdicts, and `score_sha256`. Timed scores retain numerator/denominator/ratio
plus alignment window, support, usable intents, offset, support residuals, and
residual spread. Presence-only scores retain settlement presence but set
precision to `not_resolvable`. Publication is exclusive and byte-idempotent; a
different existing score is an integrity failure. The same module exports
`verify_precision_artifact(context, value)` as the consumer-side schema,
identity, input-fingerprint, score-hash, count/ratio, and alignment validator
for the EVIDENCE join.

`missed-arm-check.py` emits `MISSED-ARM` per issuer, ranked by contracts
missed, and exits 1 when any survive. A board lookup that fails is reported
`UNDETERMINED` and exits 2, never silently clean. Each finding names artifact-bound armed
lanes separately from unarmed commitments.

The puller publishes a byte-preserving local evidence copy. It asks
`control.operate`'s host-boundary door to import an exact verified extension,
then asks its public `eventctl.py record` door to append `evidence_pulled`; it
does not synthesize or bypass ledger writes itself.

The schema-3 audit publishes canonical `RunGrade` schema 1 JSON at the exact
`plan.artifacts.grade` destination using an fsynced temporary file, exclusive
link publication, and parent-directory fsync. An existing grade is accepted
only when byte-identical. Remote evidence reaches this point through the
verified `evidence_pulled` receipt. For a terminal laptop-hosted webcast run,
the audit records `evidence_consolidated` only after independently validating
the in-place order, book, audio, and report artifacts. It then asks
`eventctl.py record` for `graded`; neither path invents a host transfer.

`scripts/audit-day.py --date YYYY-MM-DD` is the derived date reconciliation.
Its event denominator is the union of dated `slate/latest.json` entries and
every immutable RunPlan whose UTC start falls on the date. It verifies each
lifecycle through `eventctl`, and invokes the single-run auditor for every
completed, aborted, or failed attempt while continuing siblings. Descopes stay
in the denominator as recorded non-runs. Active, blocked, corrupt,
unauditable, and operationally failed attempts make the date fail nonzero; an
empty denominator also fails. The byte-stable `var/audits/<DATE>.json` index
binds each row to its RunPlan, grade, and current lifecycle-tail fingerprints
and aggregates accounting, audio volume, detection count, cap-depth classes,
and measured detection/wire-to-exhaustion timings. It is rebuildable derived
state and never rewrites an immutable grade. The reconciliation also consumes
each run's proof-bearing alert status. Any unacknowledged alert keeps the date
red even when the evidence grade itself is valid, and the index records only
the alert identity, class, delivery ambiguity, and acknowledgement state (not
the potentially sensitive message body).

Above the attempt rows, the index collapses lane suffixes into one market
event and emits exactly one `COVERED`, `DESCOPED`, or `MISSED` classification.
Any valid grade with positive audio frames makes the event `COVERED`; a typed
descope makes it `DESCOPED` when no lane covered; otherwise the event is
`MISSED`, including a slate event with no RunPlan at all. Each event retains
its compact attempt statuses and repository-relative links to its slate,
RunPlans, receipt ledgers, and grades. A missing event, a problematic covered
attempt, or a problematic descope keeps the date red.

## Side effects and exclusive resources

Grading reads immutable run/evidence artifacts and may request notification
through `platform.notify`. The puller is read-only on the box and never mutates
reports, configs, corpus, or trading state. Its one typed mutation is the local
append-only `evidence_pulled` receipt, gated by a terminal run and successful
RunPlan/ledger verification.
The typed precision scorer's only mutation is exclusive publication of
`grade/precision-score.json`; it never edits the RunPlan, report, grade, tapes,
or lifecycle ledger.

## Failure contract

Missing/unreadable/internally inconsistent report fails loud. Missing alignment
degrades keyword validation to count mode and is labelled; it never fabricates
precision/recall. Diagnostic tool failure does not rewrite the operational
verdict.
Typed scoring is stricter: missing or mutated identity-bound inputs fail before
publication, and timed alignment ambiguity exits 3 without an artifact.

For the schema-3 audit, a validation failure creates neither a grade nor a
lifecycle receipt. Missing, partial, dropped, corrupt, identity-divergent, or
report-disagreeing evidence is a failure. A valid grade may still carry an
operational `fail` (for example, a finalized run with unresolved orders); this
preserves the distinction between trustworthy evidence of a failed operation
and evidence that cannot be trusted.

## Parallel work boundary

Verdict rules and output schema stay here. Evidence extraction belongs to
evidence nodes; promotion is a separate reviewed config/benchmark change;
notification delivery remains platform-owned.

## Verification

Run `scripts/run-verdict.sh --check`, `python3 tests/score_precision.py`, and
focused fixture runs for `scripts/validate-from-log.py`. Deeper model miss autopsy belongs to
`evidence.benchmark`.

`python3 scripts/missed-arm-check.py --check` runs offline on temp fixtures. Its
2026-08-11 block reconstructs HIMS 26AUG10 exactly — a webcast lane whose report
sits at the canonical path and a phone lane whose report exists ONLY under
`box/` — and asserts: both lanes staged from the real
`<issuer>-<date>-phone-arm.toml` filename, the box report counted (2 orders),
both lanes' frames summed, health resolved to the best lane by name, a stale
canonical copy alongside a `box/` copy counted **once**, and a `-SWEEP` run
excluded from the numerator.
The same self-test includes the M5 regressions: a 1¢ config whose filename
matches an arm pattern but has no run/report/journal cannot supply posture, and
an ETOR-shaped candlestick series whose early interval traded 26,496 contracts
while its final interval is zero remains traded for the window.
Venue/tool rehearsal probes (`TTSREHEARSAL`, `Q4CHIME`) are also excluded from
the issuer denominator, and an injected board transport failure must exit 2.

`python3 tests/score_precision.py` proves global long-offset alignment,
ambiguous-cluster refusal, timestamped precision with retained residual-spread
diagnostics, presence-only non-precision, typed config/report fingerprint
binding, deterministic score hashing, exclusive publication, byte-identical
idempotency, and divergent-score refusal.

`scripts/pull-box-state.sh --check` is a network-free temp-repository fixture.
It exercises both `--date` and `--event`, a complete terminal box extension of
an existing laptop preflight prefix, the local receipt-tail idempotency case,
byte-divergent evidence refusal, a legacy-only event, active and descoped runs
that remain inventory-only, a failed run that must publish, a corrupt RunPlan
that must publish nothing, and dry-run non-mutation. For `--prelaunch` it also
pins that a pre-launch run publishes, records no `evidence_pulled`, and is
idempotent — and that a run which reached `launched` is still refused.

Its assertions go through the `must` / `must_not` helpers, and that is a
correctness requirement rather than style. macOS ships bash 3.2, where a failing
`[[ ... ]]` does **not** trigger `set -e` (a failing `[ ... ]` does). Every
assertion here used to be a bare `[[ ... ]]`, so on the operator's own laptop the
whole gate passed vacuously: measured 2026-08-14, with the `--prelaunch`
publication path deliberately disabled, `--check` still printed PASS. Do not
reintroduce a bare `[[ ]]` assertion in this file.

`scripts/pull-box-state.sh --dry-run` performs only the remote read-only
inventory and writes no local path. Exit codes are the machine surface: `0`
pulled/verified (or dry-run listed), `1` transport/integrity/divergence
refusal, `2` bad or missing arguments, `3` no matching state **or typed run**.
**Read those without a pipe** — `head`/`tail` mask `$?`.

`python3 tests/run_audit.py` builds complete, network-free schema-3 attempts
and covers: a filled order; a zero-intent/zero-trade run; a correct no-attempt
on an empty book; positive
cap depth exhausted after detection with both monotonic timings; positive
depth with no exhaustion; a tampered order hash; illegal transition ordering;
a corrupt book commit; report/order disagreement; audio commit tampering;
valid one- and two-session FIX tapes (including a zero-observation session and
an exact repeated frame); missing, truncated, mutated, projection-divergent,
identity-divergent, and unknown-field FIX rows; missing or prefix-mismatched FIX
commit receipts; report/close-summary disagreement; contradictory REST tapes,
commits, or health claims; byte-identical grade/lifecycle idempotency; and the
no-mutation guarantee on validation failure. The order fixture includes a
literal compact-serde hash vector so the Python verifier cannot silently adopt
a different canonical JSON encoding.

`python3 tests/audit_day.py` proves sibling continuation, a loud empty
denominator, date filtering, corrupt-plan retention for deterministic RunIds,
active and unauditable failures, operational-fail propagation, aggregate
performance counts/timings, unacknowledged-alert propagation, and byte-stable
re-publication. It also proves lane collapse, all three event classifications,
a slate event with no attempt becoming explicit `MISSED` with an evidence
link, and the scheduled wrapper running audit-day after missed-arm and paging
an injected audit failure.

The scheduled leg counts as validated only per the CLAUDE.md unattended rule:
launchd-fired (`launchctl kickstart gui/$(id -u)/com.earshot.missed-arm`, not
a manual run), killed with a real SIGTERM mid-run (the checker is read-only —
it must die clean with no partial state), under launchd's minimal env.
Rehearsed 2026-08-04: see the log excerpt in `missed-arm.log` and the
VERIFICATIONS.md pin.

## Migration status

Box evidence now crosses the host boundary with run identity and lifecycle
intact, schema-3 runs have a deterministic structured `RunGrade`, and the
canonical `audit-day` operation produces a stable date-level performance
index. Legacy missed-arm grading remains for pre-RunPlan historical events;
keyword validation intentionally exits zero as a report.
M6's event classifications and scheduled post-missed-arm invocation are
implemented; `opsctl audit-day --date` already exposes this same index.
The M3 scorer producer and claim-comment consumer contract are implemented.
The EVIDENCE-owned `run-audit.py` still emits `precision=not_resolvable` and
must verify/join `grade/precision-score.json` before M3 is production-complete.
