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
It also owns `audio_evidence::{AudioEvidenceBank, AudioEvidenceTap}`: the
run-scoped, in-process banker for the exact post-preprocessor `AudioFrame`
values admitted to the detector path. Runtime assembly owns the tap's placement
and lifecycle wiring; capture owns the evidence protocol and artifacts.
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

The in-process banker receives an immutable `AudioEvidenceIdentity` (`run_id`,
`event_id`, and canonical `plan_sha256`) and six explicit absolute paths in
`AudioEvidencePaths`: `raw`, `frames`, `manifest`, `commit`, `lock`, and
`wav_dir`. Optional `source_labels` name numeric `SourceId` legs without
changing their identity. It never discovers paths or identity from the
environment.

`AudioEvidenceTap::try_record_accepted(&AudioFrame)` is bounded and
non-blocking. The integration ordering is load-bearing:

1. preprocessing finishes resampling and stamps `preproc_ts`;
2. it clones that post-preprocessor frame and calls `try_record_accepted`;
3. only `Ok(())` permits the original frame to enter the lossless
   preprocessor-to-chunker ring; and
4. the pipeline increments its published-frame count only after that ring send
   returns.

Thus a successful close can prove
`bank accepted == pipeline published == writer persisted`. A full, closed,
invalid, or asynchronously failed evidence tap is a run-fatal health failure,
not a log warning. The preprocessing stage must return both its published count
and first typed tap error. After that first error it forwards neither the failed
frame nor any later frame, but continues draining its input until upstream
closes so a live source cannot strand the stage or make join accounting
misleading. Assembly continuously polls `AudioEvidenceTap::health`, cancels the
source, drops every fanout tap, joins every preprocessing thread, and calls
`abort` on any stage or bank error. Only an entirely healthy teardown passes
the summed published count to `AudioEvidenceBank::finish`.

## Output contracts

Each leg yields `<label>.wav` plus frame-level `<label>.jsonl`; the harness
emits a capture report with leg metadata, counts, jitter/stall statistics, and
cross-leg observations. The target `CaptureManifest` binds hashes, config,
clock/vantage, and tool revision to every artifact.

For in-process runtime evidence, the binary `raw` tape is authoritative. It
stores mono PCM16 samples plus sample rate, numeric source, source-local
sequence, capture timestamp, preprocessor timestamp, record index, per-frame
PCM hash, and a chained record hash. `frames` is a strict newline-terminated
JSONL sidecar carrying the same metadata and raw byte offsets, but no duplicate
PCM. Clean shutdown appends a durable pending-close seal to both.

After strict replay, the bank derives playable WAV segments under `wav_dir`.
Fanout legs remain separate by numeric `SourceId`; a sample-rate change,
non-consecutive sequence, capture-clock regression, or preprocessor-clock
regression starts a new explicitly reasoned segment. The manifest binds every
segment's decoded-PCM hash, sample count, WAV byte hash, frame/sequence range,
clock range, source label, and rate back to the raw tape.

Completion is two phase. `manifest` always states `complete=false`; for a
healthy run it states `pending_commit`. Only an atomically published, fsynced
`commit` receipt with `complete=true` is the commit point, and that receipt is
bound to the byte lengths and SHA-256 fingerprints of raw, sidecar, manifest,
and every WAV. `verify_audio_evidence(manifest, commit)` replays the hash chain,
checks sidecar parity, re-derives segment boundaries, decodes WAV samples, and
validates every fingerprint before reporting completion. `replay_audio_evidence`
and `inspect_audio_evidence_raw` remain available for honest recovery and
inspection of uncommitted crash remnants.

## Side effects and exclusive resources

It opens source sockets/processes, writes potentially large audio/timestamp
artifacts, and may consume public or paid media. Output directories, leg
labels, ports, and capture sessions are exclusive per run. The in-process bank
holds a non-blocking process lock for the writer's full lifetime, creates raw
and sidecar paths with create-new semantics, and refuses existing raw, sidecar,
manifest, commit, pending-receipt, or WAV-directory artifacts. A restart cannot
overwrite a partial run.

## Failure contract

Label collisions, source construction failure, zero/invalid media, artifact
write failure, or safety timeout are explicit per-leg/run outcomes. A partial
capture is retained as partial evidence and never labelled a complete bakeoff.

The runtime bank additionally poisons health on bounded-queue rejection,
post-close use, invalid frame fields, writer disconnection, short/truncated
records, fsync failure, surviving/in-flight tap handles, count disagreement,
sidecar disagreement, WAV sample disagreement, or corrupt fingerprints. The
writer is dedicated and the producer never blocks. Periodic full sync bounds
the crash-loss window; a process crash can leave a valid prefix without a seal,
which inspection reports as partial. `finish` requires nonzero media and
bounded producer quiescence. All failure paths retain artifacts but publish no
completion receipt.

## Parallel work boundary

Recording mechanics and capture manifest stay here. Source protocols remain
adapter-owned; corpus transformation starts from immutable capture artifacts;
measurement and admission consume but do not mutate them.

`src/audio_evidence.rs` does not import pipeline, source, detector, RunPlan, or
telemetry internals. It depends only on the stable runtime vocabulary
`AudioFrame`, `SourceId`, and `MonotonicNs`. Adding its module declaration,
placing the tap at the post-preprocessor ring door, supervising health, and
coordinating shutdown belong to `runtime.assembly` and require that node's
claim.

## Verification

Run `cargo test capture_bakeoff::`, `cargo test --test bench_fixture`, and a
short replay-based capture. Validate WAV sample counts against JSONL sequence/
timestamp records and hash the resulting artifact set.

For the in-process bank, run `cargo test --lib audio_evidence` after runtime
assembly exports the module and `cargo test --test audio_evidence`. The focused
suite covers exact PCM/timestamp replay, bounded queue poison, asynchronous
writer failure, corruption and truncation, process-lock/restart exclusion,
fanout/rate/sequence segmentation, producer quiescence, invalid frames,
crash-partial inspection, and bit-exact WAV parity. A passing producer test
must still exercise the real pipeline ordering; evidence tests alone cannot
prove assembly placed the tap on the correct side of the ring.

## Migration status

The harness creates useful structured per-leg files, but CLI construction lives
in `src/main.rs`, provenance is split across config/log/report, and staging is
the prepare node's compatibility script. The in-process bank now implements a
versioned, byte-bound manifest/commit protocol behind a small public API; it is
not operational until runtime assembly exports the module, supplies the six
RunPlan paths, inserts the tap at the documented ring door, supervises poison,
and closes it after producer quiescence. The older capture-bakeoff artifacts
still need convergence onto one versioned `CaptureManifest`.
