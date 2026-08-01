# Node: `evidence.corpus`

## Job

Convert immutable raw captures and transcripts into verified, labelled corpus
items with reproducible manifests.

## Owns

Normalization/degradation in `scripts/corpus-process.sh` and
`scripts/corpus-degrade.sh`; alignment and validation in
`scripts/corpus-align.py` and `scripts/corpus-verify.py`; manifest generation in
`scripts/manifest-from-words.py` and `scripts/manifest-rescale.py`;
transcript/label/structure helpers in
`scripts/transcript-fetch.py`, `scripts/section-label*.py`, and
`scripts/structure-analyze.py`; the versioned `corpus/manifests/` and
`corpus/structure/` datasets; and their structural interpretation in
`docs/CALL-STRUCTURE.md`. Raw, telephony, transcript, and bridge-probe
surfaces belong elsewhere or are generated.

## Does not own

It does not record live sources, alter the detector, choose thresholds, run
production arms, score a benchmark, or present experimental conclusions as
ground truth.

## Input contracts

Inputs are immutable audio with provenance, transcript/word timing source,
call/event identity, split assignment, label rules, and explicit processing
parameters. Human labels and automated alignment remain distinguishable.

## Output contracts

Outputs are normalized WAV/audio, word timings, structure/section labels, and
`corpus/manifests/*.toml` entries matching `src/bench/manifest.rs`. Every item
preserves source, duration, sample rate, split, occurrences, and alignment
quality.

## Side effects and exclusive resources

It reads raw/public media, may run transcription models, and writes derived
corpus files. A corpus call ID and output path are exclusive; raw artifacts are
append-only and never overwritten by a transformation.

## Failure contract

Missing provenance, invalid audio, unverifiable alignment, duplicate call ID,
or schema mismatch rejects the item or marks it pending. Automated timing
uncertainty must not be silently upgraded to hand-labelled truth.

## Parallel work boundary

Workers may add different call IDs independently. Manifest schema or shared
label rules require one corpus-node change; benchmark code consumes manifests
read-only and must not patch data during scoring.

## Verification

Run `python3 scripts/corpus-verify.py <manifest>` for each changed manifest,
focused helper self-checks where present, and
`cargo test --test bench_fixture` to validate Rust manifest consumption.

## Migration status

Versioned manifests and processing scripts exist, but raw/derived/provenance
relationships are convention-based and some entries remain `.pending`. Add a
corpus catalog with hashes and derivation lineage so parallel additions cannot
silently reuse or overwrite an identity.
