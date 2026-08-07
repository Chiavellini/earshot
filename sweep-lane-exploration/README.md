# sweep-lane-exploration

**Question:** when a competitor sweeps a mention market, is there money in what
happens *next* — and can we make it without winning the audio race?

Requested 2026-08-06. Offline study only. Read-only against production, writes
only inside this directory.

## Isolation guarantee

Nothing under `configs/`, `scripts/`, `src/`, `docs/`, `state/`, `slate/`,
`architecture/`, or the repository root is modified by anything in this
directory. `state/` and `pm-expansion-exploration/` are **read-only inputs**.
We deliberately do **not** call `scripts/fetch-tape.sh` — it writes
`state/<EVENT>/tape-*.json`, which would break this guarantee; `fetch_tapes.py`
here writes only into `tapes/`.

No orders are placed. No `--i-am-live`, no `--fillable`, no probe-ledger touch.
Network access is the **public, no-auth, read-only** Kalshi endpoints
`GET /markets/trades` and `GET /markets`, plus — since round 2 — an
**authenticated but read-only** WebSocket subscription to `orderbook_delta` via
`record-book.sh`. That wrapper runs the existing `examples/book-tape.rs`, which
references no order endpoint, no `OrderRegistry` and no STOP file, and writes
only into `book-tapes/`.

This directory is confirmed **not** governed by `architecture/nodes.toml`'s
`governed_globs` — no node entry, no contract doc, no `claims.toml` claim, no
doc-lint obligation while it stays exploratory. ⚠ Note `govern_root_files =
true`: nothing from this work may be written to the repository root.
`git status` should never show a diff outside this directory from this work.

⚠ **Do not run this against a live event host.** `chiavellini.md` §7.5: parallel
investigation agents drove the box to load 100 during a live arm and cost 9,395
dropped audio frames. This study is laptop-local and must not run during an
armed call.

## Two rounds, two different trades

The distinction the lane turns on is **where in a repricing you enter.**

| | round 1 — AFTER | round 2 — DURING |
|---|---|---|
| entry | once the repricing has finished | between wave-1 and the decisive wave |
| arms | FOLLOW / FADE / LEVEL / MISSED | one arm |
| verdict | all NOT-RESOLVED; 94.8% of completed sweeps leave nothing to buy | TRAIN +21.0¢ (contaminated); **holdout sealed** |
| files | `PRE-REGISTRATION.md`, `score_arms.py`, `out/scoreboard.md` | `PRE-REGISTRATION-R2.md`, `score_r2.py`, `out/scoreboard-r2.md` |

Round 1's `EPISODE_MERGE_S = 120` glues the waves into one episode, so its
"residual" is what is left after the **last** wave. Entry *during* a repricing
was excluded by construction and is a materially different result. Round 1's
headline is scoped accordingly (see the correction at the top of
`out/scoreboard.md` and amendment A2 in `PRE-REGISTRATION.md`).

## Layout

```
README.md               this file
PRE-REGISTRATION.md     round 1 rules + amendments A1, A2
PRE-REGISTRATION-R2.md  round 2 rules + amendments A1, A2   <- the sealed holdout
lib/                    round 1: sweep_segment, fill_model, data, windows, arms
                        round 2: wave_ladder, exit_model, intra_arm
                        (every module has a --self-test)
fetch_tapes.py          public no-auth tape + settlement fetch -> tapes/
score_arms.py           round 1: four arms over TRAIN then TEST
score_r2.py             round 2: the intra-sweep arm; REFUSES the holdout early
replication.py          reproduces the round-1 prior, one correction at a time
robustness.py           round-1 S1-S6, LOEO, efficient-market null
record-book.sh          read-only Kalshi order-book recorder -> book-tapes/
analyze_book.py         replays a book tape: was there RESTING size at the trigger?
tapes/                  refetched trade tapes + settlements
book-tapes/             order-book tapes (the §R8 measurement)
out/                    scoreboard.md, scoreboard-r2.md, arms.json, grid.txt
```

## Conventions (inherited from `pm-expansion-exploration/`)

- Python 3 stdlib only. No cargo, no package, no `requirements.txt`, no pytest.
- Every module carries `self_test()` and a `--self-test` entry point returning a
  PASS/FAIL count.
- Production code is reused as a **pattern with provenance**, never modified.
  `pm-expansion-exploration/latency/lib/kalshi_sweep.py` is imported directly —
  it is already exploration-tier, not production.
- Numbers carry an epistemic label and a source. Quote no number until it is
  measured.

## Status

**Round 1 — CLOSED 2026-08-06.** Four arms, all NOT-RESOLVED; the durable
results are proportions, not P&L. Verdicts in `out/scoreboard.md`, with a scope
correction at the top and amendments A1/A2 in `PRE-REGISTRATION.md`.

**Round 2 — OPEN, holdout SEALED 2026-08-07.** One arm, frozen in
`PRE-REGISTRATION-R2.md` (amended A1–A3, all before any holdout event settled).
TRAIN measures +21.0¢/trade over 22 fills and passes all five `quant-audit`
gates — on the data it was tuned on, which is also what an overfit does.

The holdout opens at **36 forward events or 2026-10-30**, whichever comes first,
and `score_r2.py --split holdout` refuses until then. To keep it honest, the
only thing to do meanwhile is **bank data**:

```
python3 fetch_tapes.py                        # trade tapes + settlements, as events settle
./record-book.sh --event <E> --secs 5400      # the §R8 measurement — needs a live board
```

⚠ **Do not run `score_r2.py --split holdout --close-the-holdout` early**, and do
not edit `PRE-REGISTRATION-R2.md` §R2. Either burns the only clean test this
question has left — all 39 banked events are contaminated.

The single measurement that could resolve the arm *without* waiting is a book
tape on one armed event: every fill in the study assumes we would have won a
race for an offer somebody else took, and §R9 makes it a kill switch.
