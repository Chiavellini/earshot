//! Chunker — accumulates frames into fixed-duration detector chunks
//! (spec §5.3). Chunk size is the latency/accuracy dial of §3 stage 4.

use std::sync::Arc;

use crate::clock::MonotonicNs;
use crate::ring::{RingReceiver, RingSender};
use crate::types::{AudioChunk, AudioFrame, SourceId};

pub fn run(
    mut rx: RingReceiver<AudioFrame>,
    mut tx: RingSender<AudioChunk>,
    chunk_ms: u32,
) -> u64 {
    let mut chunks = 0u64;
    let mut buf: Vec<i16> = Vec::new();
    let mut first_capture_ts = MonotonicNs(0);
    let mut first_preproc_ts = MonotonicNs(0);
    let mut last_capture_ts = MonotonicNs(0);
    let mut sample_rate_hz = 0u32;
    let mut source = SourceId(0);
    let mut chunk_samples = 0usize;

    let seal = |buf: &mut Vec<i16>,
                    chunks: &mut u64,
                    tx: &mut RingSender<AudioChunk>,
                    first_capture_ts: MonotonicNs,
                    first_preproc_ts: MonotonicNs,
                    last_capture_ts: MonotonicNs,
                    sample_rate_hz: u32,
                    source: SourceId| {
        if buf.is_empty() {
            return;
        }
        let chunk = AudioChunk {
            pcm: Arc::from(buf.as_slice()),
            sample_rate_hz,
            first_capture_ts,
            last_capture_ts,
            first_preproc_ts,
            chunk_ts: MonotonicNs::now(),
            source,
            seq: *chunks,
        };
        buf.clear();
        *chunks += 1;
        // Interior stage: backpressure, never drop (drops belong to ingress).
        tx.send_blocking(chunk);
    };

    while let Some(frame) = rx.recv() {
        if buf.is_empty() {
            first_capture_ts = frame.capture_ts;
            first_preproc_ts = frame.preproc_ts;
            sample_rate_hz = frame.sample_rate_hz;
            source = frame.source;
            chunk_samples = (frame.sample_rate_hz as usize * chunk_ms as usize) / 1000;
            // One up-front reservation (+ headroom for the frame that tips
            // the buffer over chunk_samples) instead of doubling reallocs on
            // the first chunk. `clear()` keeps the capacity afterwards.
            buf.reserve(chunk_samples + frame.pcm.len());
        }
        last_capture_ts = frame.capture_ts;
        buf.extend_from_slice(&frame.pcm);
        if buf.len() >= chunk_samples {
            seal(
                &mut buf, &mut chunks, &mut tx,
                first_capture_ts, first_preproc_ts, last_capture_ts,
                sample_rate_hz, source,
            );
        }
    }
    // End of stream: flush the remainder so trailing audio is never lost.
    seal(
        &mut buf, &mut chunks, &mut tx,
        first_capture_ts, first_preproc_ts, last_capture_ts,
        sample_rate_hz, source,
    );
    chunks
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ring;

    fn frame(seq: u64, samples: usize, ts: u64) -> AudioFrame {
        AudioFrame {
            pcm: Arc::from(vec![seq as i16; samples].as_slice()),
            sample_rate_hz: 8000,
            capture_ts: MonotonicNs(ts),
            source: SourceId(0),
            seq,
            preproc_ts: MonotonicNs(ts + 10),
        }
    }

    #[test]
    fn chunks_are_exact_and_remainder_flushes() {
        let (mut ftx, frx) = ring::ring::<AudioFrame>(64);
        let (ctx, mut crx) = ring::ring::<AudioChunk>(64);
        // 10 frames of 160 samples = 1600 samples; chunk_ms 160 @ 8 kHz = 1280.
        for i in 0..10 {
            ftx.send(frame(i, 160, 1000 * (i + 1))).unwrap();
        }
        drop(ftx);
        let chunks = run(frx, ctx, 160);
        assert_eq!(chunks, 2);
        let c1 = crx.recv().unwrap();
        let c2 = crx.recv().unwrap();
        assert!(crx.recv().is_none());
        assert_eq!(c1.pcm.len(), 1280); // 8 frames
        assert_eq!(c2.pcm.len(), 320);  // remainder: 2 frames
        assert_eq!(c1.first_capture_ts, MonotonicNs(1000));
        assert_eq!(c1.last_capture_ts, MonotonicNs(8000));
        assert_eq!(c1.first_preproc_ts, MonotonicNs(1010));
        assert_eq!(c2.first_capture_ts, MonotonicNs(9000));
        assert!(c1.chunk_ts <= c2.chunk_ts);
    }
}
