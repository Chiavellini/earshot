//! `ReplaySource` — plays a WAV file with original (or scaled) timing,
//! injecting synthetic `capture_ts` at emit (spec §5.2 adapter #1).
//!
//! `capture_ts` measures *pipeline* latency only: it is stamped when the frame
//! is emitted, never backdated to the WAV timeline. Ground-truth "when was the
//! word audible" comparisons use the fixture's known burst offsets instead.

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;

use super::{
    AudioSource, LatencyClass, SourceDescriptor, SourceError, SourceExit, SourceKind,
};
use crate::clock::MonotonicNs;
use crate::ring::RingSender;
use crate::types::{AudioFrame, SourceId};

pub const FRAME_MS: u32 = 20;

pub struct ReplaySource {
    pcm: Vec<i16>, // whole file in memory: no I/O jitter mid-replay (1 h @ 8 kHz ≈ 58 MB)
    sample_rate_hz: u32,
    speed: f32, // 0 = as fast as possible
    source_id: SourceId,
}

impl ReplaySource {
    pub fn from_wav(path: &Path, speed: f32, source_id: SourceId) -> Result<Self, SourceError> {
        let mut reader = hound::WavReader::open(path)?;
        let spec = reader.spec();
        if spec.channels != 1 || spec.bits_per_sample != 16 {
            return Err(SourceError::Io(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "replay expects mono 16-bit WAV, got {} ch / {} bit",
                    spec.channels, spec.bits_per_sample
                ),
            )));
        }
        let pcm = reader
            .samples::<i16>()
            .collect::<Result<Vec<_>, _>>()?;
        Ok(ReplaySource {
            pcm,
            sample_rate_hz: spec.sample_rate,
            speed,
            source_id,
        })
    }

    fn samples_per_frame(&self) -> usize {
        (self.sample_rate_hz * FRAME_MS / 1000) as usize
    }
}

#[async_trait]
impl AudioSource for ReplaySource {
    fn descriptor(&self) -> SourceDescriptor {
        SourceDescriptor {
            kind: SourceKind::Replay,
            codec: "pcm_s16le",
            nominal_latency: LatencyClass::Synthetic,
            clock_domain: "file",
        }
    }

    async fn run(
        self: Box<Self>,
        mut tx: RingSender<AudioFrame>,
    ) -> Result<SourceExit, SourceError> {
        let spf = self.samples_per_frame();
        let frame_dur = Duration::from_millis(FRAME_MS as u64);
        let paced = self.speed > 0.0;
        let start = tokio::time::Instant::now();

        for (n, window) in self.pcm.chunks(spf).enumerate() {
            if paced {
                // Absolute deadlines: per-frame timer error never accumulates.
                let deadline = start + frame_dur.mul_f64(n as f64 / self.speed as f64);
                tokio::time::sleep_until(deadline).await;
            }
            let frame = AudioFrame {
                pcm: Arc::from(window), // per-frame alloc accepted in M0; FramePool seam for M1
                sample_rate_hz: self.sample_rate_hz,
                capture_ts: MonotonicNs::now(),
                source: self.source_id,
                seq: n as u64,
                preproc_ts: MonotonicNs(0),
            };
            if paced {
                // Live semantics: never block the capture path; a stalled
                // consumer shows up as a loud drop counter.
                if tx.send(frame).is_err() {
                    tracing::warn!(seq = n, "ring full: frame dropped");
                }
            } else {
                // Fast mode is offline regression replay: lossless, so
                // backpressure blocks the replayer instead of dropping.
                let mut frame = frame;
                loop {
                    match tx.try_send(frame) {
                        Ok(()) => break,
                        Err(crate::ring::RingFull(f)) => {
                            frame = f;
                            tokio::task::yield_now().await;
                        }
                    }
                }
            }
        }
        Ok(SourceExit::EndOfStream)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixture::FixtureSpec;
    use crate::ring;

    #[test]
    fn fast_mode_emits_all_frames_in_order() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("f.wav");
        FixtureSpec::standard().write_wav(&path).unwrap();

        let src = Box::new(ReplaySource::from_wav(&path, 0.0, SourceId(0)).unwrap());
        let (tx, mut rx) = ring::ring::<AudioFrame>(1024);

        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        let consumer = std::thread::spawn(move || {
            let mut frames = Vec::new();
            while let Some(f) = rx.recv() {
                frames.push(f);
            }
            frames
        });
        let exit = rt.block_on(src.run(tx)).unwrap();
        assert_eq!(exit, SourceExit::EndOfStream);

        let frames = consumer.join().unwrap();
        assert_eq!(frames.len(), 500); // 10 s / 20 ms
        for (i, f) in frames.iter().enumerate() {
            assert_eq!(f.seq, i as u64);
            assert_eq!(f.pcm.len(), 160); // 20 ms @ 8 kHz
        }
        for pair in frames.windows(2) {
            assert!(pair[1].capture_ts >= pair[0].capture_ts);
        }
    }

    #[test]
    fn rejects_stereo() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("stereo.wav");
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: 8000,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut w = hound::WavWriter::create(&path, spec).unwrap();
        for _ in 0..320 {
            w.write_sample(0i16).unwrap();
        }
        w.finalize().unwrap();
        assert!(ReplaySource::from_wav(&path, 1.0, SourceId(0)).is_err());
    }
}
