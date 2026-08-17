---
as_of: 2026-08-17
branch: webcast-lane (12 commits ahead, nothing pushed)
authority: docs/SYSTEM-STATE.md (the defect ledger) + architecture/claims.toml
---

# Handoff — reliability program, state as of 2026-08-17

For the agent picking this up. Read `docs/SYSTEM-STATE.md` in full afterwards;
this page tells you *how to read it* and *where the remaining risk actually
lives*. It does not restate the ledger.

## 1. The one-paragraph status

Over 2026-08-12 → 08-17 the program stopped being "make the trading work" and
became "make the operating chain incapable of silently lying." Most of what
landed is **control-plane** reliability: host-boundary transport, gate honesty,
posture declaration, alert routing. That plane is now proven end-to-end on the
box — a webcast attempt was compiled on the laptop, dispatched, preflighted on
the box, pulled back, scheduled, and read back as a LOADED timer with **zero
hand transport**. What did **not** change: the canonical evidence tree has
still never been populated by a live run (**E1**), money policy is still
unruled (**§4 O1–O5**), and the laptop's own scheduler is currently dead
(**L9**). Do not read "12 commits of reliability work" as "the system is
reliable." Read it as "the chain can now be trusted to tell you when it isn't."

## 2. What actually landed (mechanisms, not commits)

Five defect classes were attacked. Knowing the *class* matters more than the
individual fixes, because the next defects will be in the same classes.

**(a) Two hosts, two copies of the truth.** Every run carries an immutable
schema-3 RunPlan plus a hash-chained append-only `control/receipts.jsonl`. The
box and the laptop each hold one. There was no supported way to move a
*pre-terminal* receipt in either direction, so the drill hit `rsync` by hand
three times in one session — which no skill can do and which invites a
hand-written receipt. Closed both ways: `pull-box-state.sh --prelaunch`
(box→laptop) and `box-webcast-install.sh --dispatch-only` (laptop→box), both
over the already-tested direction-agnostic `run-host-boundary.py reconcile`
prefix primitive. Neither weakens a gate; neither records a receipt.
`--supersede-legacy` (E10) and SSH connection multiplexing (E13) came out of
proving it. **Transport is mandatory, not a convenience** — the same config
compiled on both hosts yields byte-different plans because `created_utc` sits
inside `plan_sha256` (measured).

**(b) Gates that could not fail — the meta-defect.** This class caused more
damage than any product bug and you should assume more of it exists.
Instances found: a selftest that passed with the feature under test
*deliberately disabled*, because bash 3.2 (the only bash macOS ships) does not
trip `set -e` on a failing `[[ ]]` (**E9**); a test asserting exit code 3 when
three unrelated causes also produce 3 (**E8**); a wedge test whose stub
`exit 0`'d where the real thing never exits (**C7**); a scheduler readback
selftest that imports only `re` and therefore could never see the interpreter
breakage that killed the scheduler (**L9**). **Standing rules that came out of
it, now in the ledger: assert with `must`/`must_not`, never a bare `[[ ]]`;
never pipe a gate through `head`/`tail` (masks `$?`); every new assertion must
be proved red by tampering before you believe it green.**

**(c) The notification killed the run it was warning about (E12).** `alertctl`
writes alert facts as rows in the RunPlan's own hash chain. For a run executing
on another host that is fatal — both hosts append at the same index, the chain
forks permanently, and the attempt becomes undispatchable, unpullable,
unrecoverable. Measured: one transient `opsctl schedule` failure wrote nine
alert rows into the laptop copy and destroyed a prepared box attempt. Fixed at
two layers (routing in `schedule-nightly.sh`, refusal in `alertctl.py`).

**(d) Posture was hardcoded (O6).** Every scheduled webcast run — both
installers, not just the box — forced `--i-am-live --fillable`. There was no
scheduler-level non-trading posture (`capture_only` is `not keywords` by
construction, so it cannot exercise the detector at all). Now `[event]
rehearsal` in the config, derived by one shared entry point
(`schedule-proof.py posture`), enforced at three layers: unit build, LOADED
readback, and `arm-on-url.sh` hand-invocation. It lives in the config because
the RunPlan's field set is exact-match schema; `config_sha256` already binds it.

**(e) The laptop is a second-class host.** L7 (no PATH in the plist) killed the
first live firing of the typed scheduler. L9 (Homebrew `pyexpat` linked against
a newer expat than macOS ships) means `import plistlib` fails, so **the laptop
cannot schedule a webcast event at all right now**. The box is unaffected —
systemd, no plist — which is exactly why the 08-16/17 drill ran end to end
while this was broken. Detection is closed (preflight now imports what the
chain imports); **the repair is open and is the operator's call** — two routes
in the L9 entry.

## 3. Gate status right now (measured, not assumed)

| gate | state | why |
|---|---|---|
| `scripts/doc-lint.sh` | **RED, 12 errors** | *Not ours.* Every error is an untracked nightly-staged config (`as-`/`hd-`/`klar-`/`tol-`/`tgt-` for 26AUG18–19) with no registry entry. The nightly jobs commit autonomously (**L5**); these appeared mid-session. |
| `cargo test` | 29 suites green, **2 red** | `node_contract_registry_is_consistent` (same root cause as doc-lint above) and `kalshi::executor::tests::prestage_enumerates_all_tranches` (**E6**, pre-existing, client-id mismatch at `src/kalshi/executor.rs:1651`). |
| `tests/typed_scheduler_boundary.sh` | green | Extended this session with `--dispatch-only` and rehearsal-posture cases; each new assertion proved red by tampering. |
| `tests/alertctl.py` | green (14) | Two new E12 cases. |
| `pull-box-state.sh --check` | green | Extended with `--prelaunch` and `--supersede-legacy` cases. |
| `schedule-nightly.sh --check` | green | Now asserts the alert **target**, not merely that an alert was raised. |
| laptop webcast scheduling | **DEAD** | L9. Box lane fine. |

**Before you diagnose anything, run doc-lint and confirm the 12 errors are
still only nightly configs.** If they are, they are noise; if the count or the
files changed, something else moved.

## 4. Recommended diagnosis procedure

```
scripts/doc-lint.sh                                   > /tmp/dl.log 2>&1; echo "EXIT:$?"
export ORT_DYLIB_PATH=...    # cargo test SIGABRTs in sherpa_kws without it
cargo test                                            > /tmp/ct.log 2>&1; echo "EXIT:$?"
scripts/pull-box-state.sh --check                     ; echo "EXIT:$?"
bash tests/typed_scheduler_boundary.sh                ; echo "EXIT:$?"
python3 tests/alertctl.py                             ; echo "EXIT:$?"
scripts/schedule-nightly.sh --check                   ; echo "EXIT:$?"
python3 -c "import tomllib, plistlib"                 ; echo "EXIT:$?"   # L9 tell
scripts/opsctl.sh surface                                                # the supported surface
```

Then read `docs/SYSTEM-STATE.md` §3 top to bottom. The IDs are stable and the
three work orders (`SOLUTION-CAPTURE`/`-EVIDENCE`/`-OBSERVABILITY`) reference
them. **§3 ends with a "Deliberate behaviours that look like defects — DO NOT
PATCH" subsection. Read it before proposing anything**; each entry is a safety
property with a test pinning it, and two of them looked exactly like bugs
during the rehearsal.

## 5. Where the remaining risk actually is — ranked

Judgement, not a restatement. Rank by *what can silently destroy an event*.

1. **E11 — permanent ledger fork by mis-ordered close (OPEN).** The most
   dangerous open item, because ordinary correct-looking operator action
   reaches it and there is no recovery short of `eventctl init`. Only mitigated
   (the refusal now names the cause). The real fix is host-awareness in
   `eventctl record` — refuse a laptop-side write to a run whose
   `execution_host_id` is not this host without a fresh pull. That is a
   `control.operate` change. **Order of operations, always: schedule → pull →
   close → dispatch.**
2. **E1 — the canonical `var/runs/` tree has never held live evidence.** Every
   real capture to date landed in legacy `state/<E>/`. Every grader, audit and
   skill downstream assumes the canonical tree. Until one live run populates it,
   the entire evidence plane is untested against reality. Related: E4 (phone
   in-process audio parity never run live), E5, M3, M6.
3. **L9 — laptop scheduler dead.** Blocks any laptop-hosted webcast event
   today. Detection closed; repair needs the operator (17 live processes on
   that interpreter, including a real-stake trader loop).
4. **E13 — other SSH callers still unmultiplexed** (`box-webcast-install.sh`,
   `deploy-box.sh`, `box-ready.sh`) and can trip the same `MaxStartups` wall
   that cascaded into E12.
5. **C3–C6** — venue-derived redial, operator-queue escalation, the T-90…T-30
   probe window against T-30 bridges, host contention with no guard (C5 has a
   built delivery door in `platform.notify`; CAPTURE still owes the guard), and
   the recorder-tool bypass.
6. **M1/M2/M4** — no supervised live run monitor, alerting not migrated off
   email-first on legacy paths, no live read-through to the box during a run
   (`STALE_PREFIX_POSSIBLE` between dispatch and terminal pull).
7. **E6 — verification debt.** The full matrix has never been run and
   classified; the branch is 240+ commits ahead of `main` with no immutable
   release artifact.

## 6. Non-negotiables

- **Never point a recorder/probe at a tradeable call.** `bridge-probe` /
  `chain-capture` bank corpus only. To cover an event, ARM it.
- **Money policy is out of scope.** `src/risk.rs` hard caps and SYSTEM-STATE §4
  values are the operator's, not an agent's. O1–O5 stay open until ruled.
- **Never weaken a gate to make transport work; never hand-write a lifecycle
  receipt.** Both were live temptations during this work.
- **Never re-run `preflight-event.sh` against a real event** — it rebuilds
  `state/<E>/earshot-pinned` from the current worktree, in place.
- **Do not touch AS 26AUG18 or any real event's configs, plans, timers or
  state.** The 26AUG18–19 configs in `git status` are nightly-staged real
  events.
- **Never `git add -A`** — nightly jobs commit autonomously mid-session (L5);
  re-check `git status` and use exact pathspecs before every commit.
- Branch stays `webcast-lane`. Do not rebase.
- Claim your node in `architecture/claims.toml` before editing (`AGENTS.md`);
  one open claim per node, exact file paths, never globs.
- Log every gate to a file and echo `EXIT:$?`. Never pipe through `head`/`tail`.
- zsh: unquoted `$var` does not word-split, and a bare `====` word triggers
  `=cmd` expansion. Quote things.

## 7. Known open defects deliberately left unfixed, so you don't re-find them

- `arm-on-url.sh` uses BSD `date -r` (works on macOS, not portable).
- `pcm-gate` runs once while the resolver retries.
- `launch-webcast.sh`'s identity gate lives only inside the live branch — it
  cannot be exercised without faking receipts, which is forbidden.
