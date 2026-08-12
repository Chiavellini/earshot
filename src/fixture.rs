//! Synthetic WAV fixtures: silence with tone bursts at known offsets.
//!
//! Because burst onsets are exact, the fixture doubles as ground truth for
//! detection-latency measurement (spec §9.1) without committing audio assets.

use std::f32::consts::TAU;
use std::path::Path;

#[derive(Debug, Clone, Copy)]
pub struct Burst {
    pub onset_ms: u32,
    pub dur_ms: u32,
}

#[derive(Debug, Clone)]
pub struct FixtureSpec {
    pub sample_rate_hz: u32,
    pub total_ms: u32,
    pub tone_hz: f32,
    pub amplitude: f32, // 0..=1
    pub bursts: Vec<Burst>,
}

impl FixtureSpec {
    /// The standard e2e fixture: 10 s of 8 kHz audio, 500 ms bursts at 2/5/8 s.
    pub fn standard() -> Self {
        FixtureSpec {
            sample_rate_hz: 8000,
            total_ms: 10_000,
            tone_hz: 440.0,
            amplitude: 0.6,
            bursts: vec![
                Burst {
                    onset_ms: 2000,
                    dur_ms: 500,
                },
                Burst {
                    onset_ms: 5000,
                    dur_ms: 500,
                },
                Burst {
                    onset_ms: 8000,
                    dur_ms: 500,
                },
            ],
        }
    }

    pub fn samples(&self) -> Vec<i16> {
        let sr = self.sample_rate_hz as u64;
        let total = (sr * self.total_ms as u64 / 1000) as usize;
        let mut pcm = vec![0i16; total];
        for burst in &self.bursts {
            let start = (sr * burst.onset_ms as u64 / 1000) as usize;
            let len = (sr * burst.dur_ms as u64 / 1000) as usize;
            for i in 0..len.min(total.saturating_sub(start)) {
                let t = i as f32 / self.sample_rate_hz as f32;
                let v = (TAU * self.tone_hz * t).sin() * self.amplitude;
                pcm[start + i] = (v * i16::MAX as f32) as i16;
            }
        }
        pcm
    }

    /// Ground-truth occurrences for the bench scorer: each burst is one
    /// "occurrence" of `phrase`. Gives the harness a model-free self-test
    /// (stub detector + this manifest ⇒ recall 1.0, FA 0).
    pub fn occurrences(&self, phrase: &str) -> Vec<crate::bench::manifest::Occurrence> {
        self.bursts
            .iter()
            .map(|b| crate::bench::manifest::Occurrence {
                phrase: phrase.to_string(),
                onset_ms: b.onset_ms as u64,
                end_ms: (b.onset_ms + b.dur_ms) as u64,
            })
            .collect()
    }

    pub fn write_wav(&self, path: &Path) -> Result<(), hound::Error> {
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: self.sample_rate_hz,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::create(path, spec)?;
        for s in self.samples() {
            writer.write_sample(s)?;
        }
        writer.finalize()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wav_round_trips_with_expected_length() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("fixture.wav");
        let spec = FixtureSpec::standard();
        spec.write_wav(&path).unwrap();

        let reader = hound::WavReader::open(&path).unwrap();
        assert_eq!(reader.spec().sample_rate, 8000);
        assert_eq!(reader.spec().channels, 1);
        assert_eq!(reader.len(), 80_000); // 10 s @ 8 kHz
    }

    #[test]
    fn bursts_are_loud_and_silence_is_silent() {
        let spec = FixtureSpec::standard();
        let pcm = spec.samples();
        let rms = |range: std::ops::Range<usize>| {
            let s = &pcm[range];
            (s.iter().map(|&v| (v as f64).powi(2)).sum::<f64>() / s.len() as f64).sqrt()
        };
        assert!(rms(0..8000) < 1.0, "first second should be silence");
        assert!(rms(16_800..19_200) > 5000.0, "burst at 2s should be loud");
    }
}
