//! Energy-threshold stub detector: deterministic against the tone fixture.
//!
//! Not throwaway — it stays as the pipeline self-test long after real engines
//! land, because it exercises the full timestamp chain with exact ground truth.

use smallvec::{smallvec, SmallVec};

use super::Detector;
use crate::clock::MonotonicNs;
use crate::types::{AudioChunk, Detection, KeywordId, Tier};

pub struct EnergyStubDetector {
    threshold_rms: f32,
    refractory_chunks: u32,
    cooldown: u32,
}

impl EnergyStubDetector {
    pub fn new(threshold_rms: f32, refractory_chunks: u32) -> Self {
        EnergyStubDetector { threshold_rms, refractory_chunks, cooldown: 0 }
    }

    /// Tuned for `FixtureSpec::standard()`: fires once per 500 ms burst.
    pub fn for_standard_fixture() -> Self {
        Self::new(3000.0, 8)
    }

    fn rms(pcm: &[i16]) -> f32 {
        if pcm.is_empty() {
            return 0.0;
        }
        let sum_sq: f64 = pcm.iter().map(|&s| (s as f64) * (s as f64)).sum();
        (sum_sq / pcm.len() as f64).sqrt() as f32
    }
}

impl Detector for EnergyStubDetector {
    fn process(&mut self, chunk: &AudioChunk) -> SmallVec<[Detection; 2]> {
        if self.cooldown > 0 {
            self.cooldown -= 1;
            return SmallVec::new();
        }
        if Self::rms(&chunk.pcm) >= self.threshold_rms {
            self.cooldown = self.refractory_chunks;
            return smallvec![Detection {
                keyword: KeywordId(0),
                confidence: 1.0,
                tier: Tier::Fast,
                audio_ts: chunk.first_capture_ts,
                emit_ts: MonotonicNs::now(),
            }];
        }
        SmallVec::new()
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;
    use crate::types::SourceId;

    fn chunk(pcm: Vec<i16>) -> AudioChunk {
        AudioChunk {
            pcm: Arc::from(pcm.as_slice()),
            sample_rate_hz: 8000,
            first_capture_ts: MonotonicNs::now(),
            last_capture_ts: MonotonicNs::now(),
            first_preproc_ts: MonotonicNs::now(),
            chunk_ts: MonotonicNs::now(),
            source: SourceId(0),
            seq: 0,
        }
    }

    fn loud() -> Vec<i16> {
        (0..1280).map(|i| if i % 2 == 0 { 12_000 } else { -12_000 }).collect()
    }

    #[test]
    fn silence_never_fires() {
        let mut d = EnergyStubDetector::new(3000.0, 8);
        for _ in 0..50 {
            assert!(d.process(&chunk(vec![0; 1280])).is_empty());
        }
    }

    #[test]
    fn tone_fires_once_then_respects_refractory() {
        let mut d = EnergyStubDetector::new(3000.0, 3);
        assert_eq!(d.process(&chunk(loud())).len(), 1);
        for _ in 0..3 {
            assert!(d.process(&chunk(loud())).is_empty(), "refractory violated");
        }
        assert_eq!(d.process(&chunk(loud())).len(), 1, "should re-arm after refractory");
    }
}
