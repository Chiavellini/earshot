//! Preproc stage — M0 passthrough that stamps `preproc_ts`.
//!
//! This is the seam where G.711 decode, resampling, and VAD land (spec §5.3)
//! without any re-plumbing: they slot into the body of this loop.

use crate::clock::MonotonicNs;
use crate::ring::{RingReceiver, RingSender};
use crate::types::AudioFrame;

/// Runs until the upstream sender drops. Returns frames processed.
pub fn run(mut rx: RingReceiver<AudioFrame>, mut tx: RingSender<AudioFrame>) -> u64 {
    let mut frames = 0u64;
    while let Some(mut frame) = rx.recv() {
        frame.preproc_ts = MonotonicNs::now();
        frames += 1;
        // Interior stage: backpressure, never drop (drops belong to ingress).
        tx.send_blocking(frame);
    }
    frames
}
