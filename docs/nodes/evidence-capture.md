# Node: `evidence.capture`

## Job

Record synchronized, provenance-rich media observations without attaching
trading decisions or execution.

## Owns

Multi-source recording/reporting in `src/capture_bakeoff/`, raw tape fetching
in `scripts/fetch-tape.sh`, timestamp logging in `scripts/pdt-log.py`, the
daily unattended ABC-WNT evidence job `scripts/wnt-daily.sh` (its launchd plist
stays with `platform.runtime`, which owns `configs/launchd/**`), the
broadcast-VOD acquisition it calls, `scripts/wnt-vod-fetch.sh`, and the caption
timing sidecar `scripts/wnt-captions-parse.py`.
It owns the current non-audio bridge-probe evidence under
`corpus/bridge-probes/` and the three checked-in capture reports under
`state/`; generated WAVs and local run state remain excluded, including the
banked broadcast audio under `corpus/wnt/` (gitignored).

Acquisition here is not restricted to live sockets. `wnt-vod-fetch.sh` banks a
published recording of a broadcast that no obtainable live feed carries
(`VERIFICATIONS.md` Q-WNT-SRC, Q-WNT-VOD), with the same obligation as any
capture: record what the artifact actually is. Its `meta.json` carries the
source video identity and the standing caveat that advertisements are absent,
so downstream offsets are programme time and not wall clock.

`chain-capture.sh` belongs to this node: it chains multi-leg bridge recordings
for corpus. It is owned here rather than by `control.admission` (which owns
`bridge-probe.sh`) because it banks evidence and never classifies admission.

## Does not own

It does not select a production source, label ground truth, tune a detector,
draw latency conclusions, launch a traded arm, or classify bridge admission.

**"Does not launch a traded arm" is now enforced, not merely stated.** On
2026-08-04 this node's tools recorded two live earnings calls — Spotify via
`bridge-probe` and Progressive via `chain-capture.sh` — whose boards traded
157,217 and ~178,000 contracts while the arm path sat idle, and nothing
objected, because a recorder has no idea whether what it records is tradeable.

So a record-only tool now REFUSES when the issuer has an open Kalshi mention
board, unless `--corpus-only` is passed. The override exists because corpus
capture on a live board is legitimate; making it an explicit, greppable flag
is the difference between a decision and an accident. The check fails CLOSED:
an undetermined board lookup also refuses, since a guard that fails open on a
network hiccup is how this class returns. `bridge-probe.sh` applies it only
above 300 s, so ordinary event-day admission probing is untouched.

`chain-capture.sh` additionally stops after 3 consecutive short legs. Wall
clock cannot distinguish "still capturing" from "dialling a corpse": when
Progressive's call ended during leg 6, the loop re-dialled 17 more times and
each leg held ~12.3 s of "the conference has ended". This is the same principle
`control.supervise` states as *a dial is not a capture*.

## Input contracts

Capture receives a reviewed list of source configurations, unique leg labels,
duration/safety cap, output directory, host/vantage identity, and optional
jitter setting. Admission may request a one-leg probe but owns its
classification.

## Output contracts

Each leg yields `<label>.wav` plus frame-level `<label>.jsonl`; the harness
emits a capture report with leg metadata, counts, jitter/stall statistics, and
cross-leg observations. The target `CaptureManifest` binds hashes, config,
clock/vantage, and tool revision to every artifact.

## Side effects and exclusive resources

It opens source sockets/processes, writes potentially large audio/timestamp
artifacts, and may consume public or paid media. Output directories, leg
labels, ports, and capture sessions are exclusive per run.

## Failure contract

Label collisions, source construction failure, zero/invalid media, artifact
write failure, or safety timeout are explicit per-leg/run outcomes. A partial
capture is retained as partial evidence and never labelled a complete bakeoff.

## Parallel work boundary

Recording mechanics and capture manifest stay here. Source protocols remain
adapter-owned; corpus transformation starts from immutable capture artifacts;
measurement and admission consume but do not mutate them.

## Verification

Run `cargo test capture_bakeoff::`, `cargo test --test bench_fixture`, and a
short replay-based capture. Validate WAV sample counts against JSONL sequence/
timestamp records and hash the resulting artifact set.

## Migration status

The harness creates useful structured per-leg files, but CLI construction lives
in `src/main.rs`, provenance is split across config/log/report, and staging is
the prepare node's compatibility script. Add a single versioned
`CaptureManifest` before expanding capture postures.
