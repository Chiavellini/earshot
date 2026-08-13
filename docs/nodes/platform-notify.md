# Node: `platform.notify`

## Job

Durably require one operational alert, deliver it through bounded operator
channels, and keep delivery separate from explicit human acknowledgement.

## Owns

`scripts/alertctl.py` owns the structured request, delivery-start,
delivery-receipt, and acknowledgement facts. It anchors those facts as
idempotent compatibility observations in the RunPlan's existing append-only,
hash-chained lifecycle ledger; they never advance typed run state.
For a watcher or reconciliation failure that must remain read-only toward a
run tree, the same facts live in a mode-0600 append-only hash chain at
`<alert-dir>/ledger.jsonl`. That standalone ledger is the only supported
proof-bearing path for a never-armed event or the live monitor; neither caller
writes into `var/runs/`.

`scripts/notify.sh` remains the local mail/desktop/speech adapter.
`scripts/alert-phone.sh` is the critical fallback: it calls only the operator
number provisioned outside the repository, emits only Twilio `<Say>` plus a
pause, polls until the call is answered or definitely fails, and never dials
an event bridge, conferences a leg, plays DTMF, records, or auto-acknowledges.

## Does not own

It does not decide when an event is unhealthy, grade runs, retry jobs, discover
slate changes, or suppress the originating failure. A delivery result never
changes a caller's nonzero verdict.

## Input contracts

`alertctl.py raise --run-dir <RUN> --severity warning|critical --code <ID>
--dedupe-key <ID> --subject <TEXT> --body <TEXT>` consumes a verified schema-3
RunPlan. Event/run/plan/host identity comes only from that plan. The request ID
is the SHA-256 of plan identity plus severity, code, and dedupe key; repeating
the same request is a no-op, while changing its body/subject under the same ID
is refused. The lifecycle receipt envelope supplies the authoritative UTC
timestamp.

`alertctl.py raise --alert-dir <DIR> --scope <ID> ...` is the standalone
equivalent. Its request ID binds the stable caller scope plus severity, code,
and dedupe key. The ledger sequence, previous-receipt hash, self hash, and UTC
timestamp are verified before every read or append. `--run-dir` and
`--alert-dir` are mutually exclusive; the established RunPlan request-ID
formula is unchanged for compatibility.

## Output contracts

Every adapter invocation is preceded by an atomic, durable
`alert.delivery_started` fact. A crash after that fact is ambiguous and is
never blindly redialed. Its matching `alert.delivery` fact records
`delivered`, `failed`, or `ambiguous`; only a definite failure is eligible for
the next bounded phone attempt. The configured phone-attempt count is a
lifetime bound for the deterministic alert ID, not a fresh allowance each time
a producer repeats `raise`. A delivered or ambiguous phone outcome may be
re-paged only after `--repage-after` (default 300 seconds), and only until the
lifetime phone-attempt bound (default three) is exhausted. `repage-due` applies
that policy to every stored unacknowledged critical alert; an immediate
producer restart remains a no-op. Successful local mail/desktop delivery
cannot mask a failed critical phone page. Critical requests are delivery-green
only after the phone channel succeeds or the request has already been
explicitly acknowledged. Warning requests require only the one-shot local
adapter.

`alertctl.py ack` against the request's run or standalone alert directory is
the only acknowledgement door. Delivery—SMTP accepted, a desktop notification,
or an answered phone page—is never acknowledgement. `alertctl.py status`
exits nonzero while any request remains unacknowledged.

## Side effects and exclusive resources

It may send SMTP mail, post a macOS notification, invoke speech, or call the
operator phone. Mail credentials remain in `~/.kalshi/mail_env`; critical-call
credentials and `ALERT_TO_NUMBER` remain in `~/.kalshi/alert_env`. No secret or
phone number is written to the RunPlan or either ledger. The standalone ledger
directory is caller-selected (production default: `state/alerts`) and is never
a live-run evidence destination.

## Failure contract

Every channel failure is durable and loud. Failure of the severity-required
channel returns nonzero. An accepted but unacknowledged page remains
unresolved. Concurrent duplicate requests cannot create two sends from one
attempt reservation; an incomplete reservation is reported ambiguous.
Repeated producers cannot reset the retry cap or bypass the re-page interval.
A torn, reordered, altered, symlinked, or non-canonical standalone ledger is
refused before delivery. Notification failure never rewrites the originating
node's verdict.

## Parallel work boundary

Delivery mechanisms and receipt semantics stay here. Callers own alert policy,
severity, deduplication timing, and terminal exit behavior, and should not
reimplement SMTP/desktop delivery.

## Verification

Run `python3 tests/alertctl.py` and
`bash -n scripts/notify.sh scripts/alert-phone.sh`. The offline suite injects
adapters and proves terminal-run append, request dedupe, bounded critical
retry, lifetime retry caps, required-channel semantics, warning posture, total
failure, provider ambiguity, crash-ambiguous reservation, bounded due re-page,
standalone hash-chain integrity/read-only run behavior, explicit idempotent
acknowledgement, and post-ack delivery suppression. Production phone
verification requires a separately authorized test call to the configured
operator number.

## Migration status

The proof-bearing RunPlan and standalone contracts, phone fallback,
deterministic dedupe, explicit acknowledgement, and bounded due re-page are
implemented. RunPlan-bound schedule, study, preflight, launcher terminal, and
evidence-health failures already use this door. Remaining direct `notify.sh`
critical callers are the watcher-liveness, redial/no-media, missed-arm, and
compatibility terminal/report wrappers; informational summaries may remain
local mail.
