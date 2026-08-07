//! `Detector` — the M1 bake-off seam (spec §5.4).
//!
//! Engines (grammar-restricted Vosk, sherpa-onnx zipformer, KWS models) wrap
//! their FFI behind this trait; engines with internal async runtimes get an
//! internal thread and still present this synchronous surface, which is called
//! on a dedicated pinned OS thread.

pub mod ctc_scorer;
pub mod factory;
pub mod kws_compile;
pub mod sherpa_kws;
pub mod streaming_ctc;
pub mod stub;

use smallvec::SmallVec;

use crate::types::{AudioChunk, Detection};

pub trait Detector: Send {
    fn process(&mut self, chunk: &AudioChunk) -> SmallVec<[Detection; 2]>;

    /// Engine name as it appears in `[detector].engine`, recorded into the run
    /// report. `Detection::confidence` means different things per engine —
    /// `sherpa_kws` emits a constant 1.0 sentinel, `streaming_ctc` a real
    /// posterior — so a banked report without this cannot be scored without
    /// guessing which one produced it.
    fn engine_name(&self) -> &'static str;
}
