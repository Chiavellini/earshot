# Node: `runtime.detector.keyword_compile`

## Job

Compile reviewed keyword variants and fast prefixes into deterministic,
model-token-aligned entries without running inference.

## Owns

`src/detector/kws_compile.rs` and the prefix-safety method in
`docs/PREFIX-SAFETY.md`: SentencePiece model parsing, unigram Viterbi
segmentation, safe prefix cuts, stable labels, and keyword-file rendering.

## Does not own

Detector selection, acoustic inference, CTC scoring, event preparation,
trading policy, or corpus acceptance thresholds.

## Input contracts

Validated `KeywordConfig` values, a readable model `bpe.model`, default
threshold/boost values, and the shared `KeywordId`/`Tier` types from
`runtime.core`. Fast prefixes must be a strict text prefix of at least one
configured variant.

## Output contracts

`runtime.detector.keyword_compiler`: `TokenTable`, `compile`,
`prefix_cut`, `render_keywords`, and `parse_label`. Successful compilation
returns deterministic `CompiledEntry` values containing keyword ID, tier,
tokens, threshold, boost, and a round-trippable label. The Sherpa and
streaming-CTC constructors consume compilation; `control.prepare` consumes the
same token-table encoding capability for pre-arm variant validation. The
detector port declares this construction dependency; the current factory
reaches it through the selected engine constructor rather than calling it
directly.

## Side effects and exclusive resources

The compiler reads `bpe.model`; compilation itself is otherwise pure and has
no network, process, port, credential, or mutable-file side effects.

## Failure contract

I/O, malformed protobuf data, unencodable symbols, non-strict prefixes, and
prefixes matching no variant return `CompileError`. Factory callers refuse to
arm; they never drop a keyword or substitute a different tokenization.

## Parallel work boundary

Edits stay in the compiler and prefix-safety documentation. A change to
`CompiledEntry` or token semantics coordinates with both engine nodes,
`control.prepare`, and the detector port before implementation lands.

## Verification

Run `cargo test detector::kws_compile` and
`cargo run -- compile-keywords --config configs/m1-kws-example.toml` when the
model assets are present. Finish with `scripts/doc-lint.sh`.

## Migration status

The compiler is extracted in code and exposed as a capability contract.
The legacy detector layer document still describes it alongside both engines;
new engine work should depend on this node rather than duplicate token logic.
