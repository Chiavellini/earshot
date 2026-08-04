# Node: `control.grade`

## Job

Turn a terminal run's artifacts into an explicit operational and detection
quality verdict without changing future posture.

## Owns

Operational PASS/WARN/FAIL rules in `scripts/run-verdict.sh`, log/transcript
comparison in `scripts/validate-from-log.py`, and the missed-arm reconciliation
in `scripts/missed-arm-check.py`.

**Missed-arm reconciliation grades the run that never happened.** Its question
is one line — *we have capture audio for issuer X, X's board traded, we placed
no orders* — and it is deliberately INDEPENDENT of which upstream node broke.
It fires whether `control.discover` lost the event, `control.qualify` lost the
bridge, or the wrong tool was pointed at the call, because it reads only the
evidence on disk. An execution verdict on a run that was never launched is
still an execution verdict, which is why it lives in this node.

It keys on a **traded** board (any status), not an open one: by the time it
runs the board has usually settled — SPOT was open and repricing at 12:02Z and
closed by 15:00Z — so keying on "open now" would clear the very miss it exists
to catch. Two cry-wolf guards are load-bearing: legs under 60 s are not
coverage (PGR's legs 07–23 held ~12.3 s each after the call ended), and
`state/` run dirs are date-filtered by event-ticker suffix (an unfiltered scan
reported GEV's 2026-07-22 event as a miss on 2026-08-04).

Existed-nowhere on 2026-08-04: Spotify and Progressive both produced zero
orders on traded boards and the system said nothing, so the question had to be
asked by a human hours later.

## Does not own

It does not supervise/relaunch the run, capture missing evidence after the
fact, promote a keyword, edit thresholds/configs, update the corpus, or deliver
an alert channel itself.

## Input contracts

Operational grading consumes a terminal `report.json` and per-leg data.
Detection validation consumes the arm log, exact event config, public
transcript, and optional aligned word timestamps with declared offset.

## Output contracts

`run-verdict.sh` emits PASS, WARN, or FAIL with stable exit semantics: absent
report or zero audio is failure; surviving audio with lost legs is warning.
Validation emits per-keyword counts/timing evidence. The target is one
versioned `RunGrade` JSON retaining both provenance and uncertainty.

`missed-arm-check.py` emits `MISSED-ARM` per issuer, ranked by contracts
missed, and exits 1 when any survive. A board lookup that fails is reported
`UNDETERMINED`, never silently clean.

## Side effects and exclusive resources

Grading reads immutable run/evidence artifacts and may request notification
through `platform.notify`. It must not mutate the run report, ledger, config,
corpus, or trading state.

## Failure contract

Missing/unreadable/internally inconsistent report fails loud. Missing alignment
degrades keyword validation to count mode and is labelled; it never fabricates
precision/recall. Diagnostic tool failure does not rewrite the operational
verdict.

## Parallel work boundary

Verdict rules and output schema stay here. Evidence extraction belongs to
evidence nodes; promotion is a separate reviewed config/benchmark change;
notification delivery remains platform-owned.

## Verification

Run `scripts/run-verdict.sh --check` and focused fixture runs for
`scripts/validate-from-log.py`. Deeper model miss autopsy belongs to
`evidence.benchmark`.

## Migration status

Operational grading has good frozen fixtures, but outputs are console/exit
codes and keyword validation intentionally exits zero as a report. Publish a
structured `RunGrade` and make the daily status layer consume it instead of
grepping logs.
