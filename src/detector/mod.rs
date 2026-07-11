//! `Detector` — the M1 bake-off seam (spec §5.4).
//!
//! Engines (grammar-restricted Vosk, sherpa-onnx zipformer, KWS models) wrap
//! their FFI behind this trait; engines with internal async runtimes get an
//! internal thread and still present this synchronous surface, which is called
//! on a dedicated pinned OS thread.

pub mod factory;
pub mod stub;

use smallvec::SmallVec;

use crate::types::{AudioChunk, Detection};

pub trait Detector: Send {
    fn process(&mut self, chunk: &AudioChunk) -> SmallVec<[Detection; 2]>;
}
