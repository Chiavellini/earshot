//! Preproc stage — timestamp stamping plus optional 8 kHz → 16 kHz upsampling
//! (spec §5.3). This remains the seam where G.711 decode and VAD land.
//!
//! Resampling is a classical polyphase half-band FIR interpolator, NOT neural
//! bandwidth extension (which hallucinates high-band content and measurably
//! hurts ASR). Exactly 2× is supported: telephony 8 kHz → the 16 kHz that M1
//! ASR models expect. Group delay is TAPS/2 input samples (1 ms at 8 kHz) and
//! shows up honestly in the capture→preproc edge.

use crate::clock::MonotonicNs;
use crate::ring::{RingReceiver, RingSender};
use crate::types::AudioFrame;

#[derive(Debug, Clone, Copy, Default)]
pub struct PreprocCfg {
    /// `pipeline.resample_to_hz`: upsample frames below this rate. `None` =
    /// passthrough. Config validation guarantees 16 000 when set.
    pub resample_to_hz: Option<u32>,
}

/// Runs until the upstream sender drops. Returns frames processed.
pub fn run(mut rx: RingReceiver<AudioFrame>, mut tx: RingSender<AudioFrame>, cfg: PreprocCfg) -> u64 {
    let mut frames = 0u64;
    let mut upsampler: Option<HalfBandUpsampler2x> = None;
    while let Some(mut frame) = rx.recv() {
        if let Some(target_hz) = cfg.resample_to_hz {
            if frame.sample_rate_hz * 2 == target_hz {
                let up = upsampler.get_or_insert_with(HalfBandUpsampler2x::new);
                frame.pcm = up.process(&frame.pcm).into();
                frame.sample_rate_hz = target_hz;
            } else if frame.sample_rate_hz != target_hz {
                // Config validation limits the target to 16 kHz; a source that
                // is neither 8 k nor 16 k is a wiring bug — loud, not silent.
                panic!(
                    "preproc: cannot resample {} Hz to {} Hz (only 2x supported)",
                    frame.sample_rate_hz, target_hz
                );
            }
        }
        frame.preproc_ts = MonotonicNs::now();
        frames += 1;
        // Interior stage: backpressure, never drop (drops belong to ingress).
        tx.send_blocking(frame);
    }
    frames
}

/// Number of FIR taps used to interpolate each midpoint sample (2·K where K
/// taps sit on each side of the midpoint).
const TAPS: usize = 16;

/// Polyphase 2× interpolator: even outputs are the (delayed) input samples,
/// odd outputs are midpoints from a Hamming-windowed sinc. Carries `TAPS`
/// samples of history so frame boundaries are seamless.
pub struct HalfBandUpsampler2x {
    coeffs: [f32; TAPS],
    hist: [i16; TAPS],
}

impl HalfBandUpsampler2x {
    pub fn new() -> Self {
        let k = TAPS as f32 / 2.0;
        let mut coeffs = [0f32; TAPS];
        let mut sum = 0f32;
        for (j, c) in coeffs.iter_mut().enumerate() {
            // Ideal midpoint interpolator sinc(j − K + 0.5), Hamming-windowed.
            let x = j as f32 - k + 0.5;
            let sinc = (std::f32::consts::PI * x).sin() / (std::f32::consts::PI * x);
            let w = 0.54
                - 0.46 * (2.0 * std::f32::consts::PI * (j as f32 + 0.5) / TAPS as f32).cos();
            *c = sinc * w;
            sum += *c;
        }
        // Normalize to unity DC gain so constant input stays constant.
        for c in &mut coeffs {
            *c /= sum;
        }
        HalfBandUpsampler2x { coeffs, hist: [0; TAPS] }
    }

    /// One input frame → 2× output samples. Output sample 2i is the input
    /// delayed by TAPS/2 samples; 2i+1 is the interpolated midpoint after it.
    pub fn process(&mut self, input: &[i16]) -> Vec<i16> {
        let mut out = Vec::with_capacity(input.len() * 2);
        for &x in input {
            // Shift history: hist[0] is oldest, hist[TAPS-1] is newest (= x).
            self.hist.copy_within(1.., 0);
            self.hist[TAPS - 1] = x;

            // Even output: input aligned with the midpoint filter's center.
            out.push(self.hist[TAPS / 2 - 1]);
            // Odd output: midpoint between hist[TAPS/2 - 1] and hist[TAPS/2].
            let mut acc = 0f32;
            for (j, &c) in self.coeffs.iter().enumerate() {
                acc += c * self.hist[j] as f32;
            }
            out.push(acc.round().clamp(i16::MIN as f32, i16::MAX as f32) as i16);
        }
        out
    }
}

impl Default for HalfBandUpsampler2x {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ring;
    use crate::types::SourceId;
    use std::sync::Arc;

    fn sine_8k(freq_hz: f32, n: usize, amp: f32) -> Vec<i16> {
        (0..n)
            .map(|i| (amp * (2.0 * std::f32::consts::PI * freq_hz * i as f32 / 8000.0).sin()) as i16)
            .collect()
    }

    fn frame(pcm: Vec<i16>, seq: u64) -> AudioFrame {
        AudioFrame {
            pcm: Arc::from(pcm.as_slice()),
            sample_rate_hz: 8000,
            capture_ts: MonotonicNs(1000 + seq),
            source: SourceId(0),
            seq,
            preproc_ts: MonotonicNs(0),
        }
    }

    fn run_stage(frames: Vec<AudioFrame>, cfg: PreprocCfg) -> Vec<AudioFrame> {
        let (mut ftx, frx) = ring::ring::<AudioFrame>(64);
        let (otx, mut orx) = ring::ring::<AudioFrame>(64);
        for f in frames {
            ftx.send(f).unwrap();
        }
        drop(ftx);
        run(frx, otx, cfg);
        let mut out = Vec::new();
        while let Some(f) = orx.recv() {
            out.push(f);
        }
        out
    }

    #[test]
    #[should_panic(expected = "only 2x supported")]
    fn non_2x_rate_panics_loud_not_silent() {
        let mut f = frame(sine_8k(440.0, 160, 10_000.0), 0);
        f.sample_rate_hz = 44_100; // neither 8 k nor 16 k: a wiring bug
        run_stage(vec![f], PreprocCfg { resample_to_hz: Some(16_000) });
    }

    #[test]
    fn passthrough_when_unconfigured() {
        let pcm = sine_8k(440.0, 160, 10_000.0);
        let out = run_stage(vec![frame(pcm.clone(), 0)], PreprocCfg::default());
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].sample_rate_hz, 8000);
        assert_eq!(&out[0].pcm[..], &pcm[..]);
        assert_ne!(out[0].preproc_ts, MonotonicNs(0));
    }

    #[test]
    fn resample_doubles_length_and_rate_preserving_identity() {
        let pcm = sine_8k(440.0, 160, 10_000.0);
        let cfg = PreprocCfg { resample_to_hz: Some(16_000) };
        let out = run_stage(vec![frame(pcm.clone(), 0)], cfg);
        assert_eq!(out[0].sample_rate_hz, 16_000);
        assert_eq!(out[0].pcm.len(), 320);
        assert_eq!(out[0].capture_ts, MonotonicNs(1000));
        assert_eq!(out[0].seq, 0);
        assert_eq!(out[0].source, SourceId(0));
    }

    #[test]
    fn sixteen_k_input_passes_through_untouched() {
        let mut f = frame(sine_8k(440.0, 160, 10_000.0).to_vec(), 0);
        f.sample_rate_hz = 16_000;
        let pcm_before: Vec<i16> = f.pcm.to_vec();
        let out = run_stage(vec![f], PreprocCfg { resample_to_hz: Some(16_000) });
        assert_eq!(out[0].sample_rate_hz, 16_000);
        assert_eq!(&out[0].pcm[..], &pcm_before[..]);
    }

    #[test]
    fn dc_gain_is_unity() {
        let mut up = HalfBandUpsampler2x::new();
        let out = up.process(&[1000i16; 200]);
        // After the TAPS-sample transient, every sample must be the DC value.
        for &s in &out[TAPS * 2..] {
            assert!((s - 1000).abs() <= 1, "DC not preserved: {s}");
        }
    }

    #[test]
    fn tone_survives_resampling() {
        // 440 Hz at 8 kHz in → the 16 kHz output must still be a 440 Hz tone:
        // compare against an ideal 16 kHz sine (half-sample-offset phase, TAPS/2
        // input-sample group delay) sample-by-sample after the transient.
        let mut up = HalfBandUpsampler2x::new();
        let n = 800;
        let out = up.process(&sine_8k(440.0, n, 10_000.0));
        let delay_out = TAPS as isize; // TAPS/2 input samples = TAPS output samples
        let mut max_err = 0f32;
        for i in (TAPS * 4)..(out.len() - TAPS * 4) {
            let t = (i as isize - delay_out) as f32 / 16_000.0;
            let ideal = 10_000.0 * (2.0 * std::f32::consts::PI * 440.0 * t).sin();
            max_err = max_err.max((out[i] as f32 - ideal).abs());
        }
        // Windowed-sinc interpolation of a low tone is near-transparent; allow
        // 2% of amplitude for the short 16-tap kernel.
        assert!(max_err < 200.0, "max deviation from ideal tone: {max_err}");
    }

    #[test]
    fn state_is_continuous_across_frame_boundaries() {
        let pcm = sine_8k(440.0, 320, 10_000.0);
        let mut whole = HalfBandUpsampler2x::new();
        let one = whole.process(&pcm);
        let mut split = HalfBandUpsampler2x::new();
        let mut two = split.process(&pcm[..160]);
        two.extend(split.process(&pcm[160..]));
        assert_eq!(one, two);
    }
}
