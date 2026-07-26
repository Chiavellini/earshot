//! Pure detection-vs-ground-truth scoring (docs/BAKEOFF.md metrics). No I/O,
//! no clocks — fully unit-testable.

use serde::Serialize;

use crate::bench::manifest::Occurrence;
use crate::clock::MonotonicNs;
use crate::telemetry::DetectionRecord;
use crate::types::Tier;

/// A detection matches an occurrence when its stream position falls in
/// `[onset − pre_ms, end + post_ms]`. `post_ms` absorbs the engine decode lag
/// (audio_pos_ms marks the chunk where the hit surfaced, ~0.4 s after word
/// end — VERIFICATIONS.md M1 pin 3).
#[derive(Debug, Clone, Copy)]
pub struct MatchWindow {
    pub pre_ms: u64,
    pub post_ms: u64,
}

impl Default for MatchWindow {
    fn default() -> Self {
        MatchWindow { pre_ms: 250, post_ms: 1500 }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct TierScore {
    pub phrase: String,
    pub tier: Tier,
    pub true_positives: u32,
    pub misses: u32,
    pub false_alarms: u32,
    /// Stream positions of unmatched detections — the FA-autopsy handle
    /// (listen to the audio there: engine error, or a word the aligner
    /// missed?).
    pub false_alarm_pos_ms: Vec<u64>,
    /// Wall-clock onset→emit latencies, paced (speed=1.0) runs only.
    pub latency_ms: Vec<i64>,
    /// **Audio-domain** lag per true positive: `audio_pos_ms − occ.end_ms`, i.e.
    /// how much audio PAST the word's end the engine had to consume before it
    /// fired. Distinct from `latency_ms` in two ways that matter:
    ///
    /// 1. It is wall-clock-free, so lossless (speed=0) runs report it — the
    ///    laptop's 3.5× run variance cannot touch it.
    /// 2. On a live HLS feed audio arrives in *segment-sized bursts*, not
    ///    smoothly, so this lag is FREE when the trailing audio is already in
    ///    the same segment and costs up to a **full segment duration** when it
    ///    is not. That phase dependence — not raw compute — is what makes the
    ///    live `audio→decision` term swing (`docs/LATENCY-PROOF.md`, E0).
    ///
    /// Measured against ground truth here for the first time; the standing
    /// "~0.4 s decode lag" figure is an ONNX-metadata inference
    /// (`VERIFICATIONS.md` Q-M1 pin 3), not an observed distribution.
    pub emit_lag_ms: Vec<i64>,
    /// **Cross-engine-comparable** lag per true positive:
    /// `audio_consumed_ms − occ.end_ms` — how much audio had to ARRIVE past the
    /// word's end before the engine could act.
    ///
    /// `emit_lag_ms` above is engine-defined and must NOT be compared across
    /// engines: `sherpa_kws` reports its fed-sample counter (160 ms grid) while
    /// `streaming_ctc` reports the scoring frame (40 ms grid, earlier than the
    /// audio its encoder needed). Comparing those two directly credited CTC with
    /// ~210 ms it had not earned (`docs/LATENCY-PROOF.md`, D1b). This field is
    /// the same quantity for every engine, so it is the one to race on.
    pub consumed_lag_ms: Vec<i64>,
}

impl TierScore {
    pub fn recall(&self) -> f64 {
        let total = self.true_positives + self.misses;
        if total == 0 {
            return f64::NAN;
        }
        self.true_positives as f64 / total as f64
    }

    pub fn precision(&self) -> f64 {
        let fired = self.true_positives + self.false_alarms;
        if fired == 0 {
            return f64::NAN;
        }
        self.true_positives as f64 / fired as f64
    }
}

/// Score one call's detections for one keyword's phrase, per tier.
///
/// `first_capture_ts`: capture time of the stream's first frame — `Some` only
/// for paced runs, where `emit_ts − (first_capture_ts + onset)` is real
/// wall-clock onset→emit latency. Lossless (speed=0) runs pass `None` and get
/// accuracy metrics only.
///
/// Matching is greedy in time order, at most one detection per (occurrence,
/// tier); leftover detections are false alarms.
pub fn score_phrase(
    phrase: &str,
    detections: &[&DetectionRecord],
    occurrences: &[&Occurrence],
    window: MatchWindow,
    first_capture_ts: Option<MonotonicNs>,
) -> Vec<TierScore> {
    [Tier::Fast, Tier::Confirmed]
        .into_iter()
        .map(|tier| {
            let mut dets: Vec<&&DetectionRecord> = detections
                .iter()
                .filter(|d| d.detection.tier == tier)
                .collect();
            dets.sort_by_key(|d| d.detection.audio_pos_ms);

            let mut tp = 0u32;
            let mut latency_ms = Vec::new();
            let mut emit_lag_ms = Vec::new();
            let mut consumed_lag_ms = Vec::new();
            let mut matched = vec![false; dets.len()];
            for occ in occurrences {
                let lo = occ.onset_ms.saturating_sub(window.pre_ms);
                let hi = occ.end_ms + window.post_ms;
                let hit = dets.iter().enumerate().find(|(i, d)| {
                    !matched[*i] && (lo..=hi).contains(&d.detection.audio_pos_ms)
                });
                if let Some((i, d)) = hit {
                    matched[i] = true;
                    tp += 1;
                    // Signed on purpose: a negative value means the engine fired
                    // BEFORE the word finished (prefix-style firing), which is a
                    // real and desirable outcome, not an error to clamp away.
                    emit_lag_ms
                        .push(d.detection.audio_pos_ms as i64 - occ.end_ms as i64);
                    consumed_lag_ms
                        .push(d.detection.audio_consumed_ms as i64 - occ.end_ms as i64);
                    if let Some(t0) = first_capture_ts {
                        let emit_since_start_ms =
                            d.detection.emit_ts.saturating_since(t0) as i64 / 1_000_000;
                        latency_ms.push(emit_since_start_ms - occ.onset_ms as i64);
                    }
                }
            }
            let false_alarm_pos_ms: Vec<u64> = dets
                .iter()
                .enumerate()
                .filter(|(i, _)| !matched[*i])
                .map(|(_, d)| d.detection.audio_pos_ms)
                .collect();
            TierScore {
                phrase: phrase.to_string(),
                tier,
                true_positives: tp,
                misses: occurrences.len() as u32 - tp,
                false_alarms: false_alarm_pos_ms.len() as u32,
                false_alarm_pos_ms,
                latency_ms,
                emit_lag_ms,
                consumed_lag_ms,
            }
        })
        .collect()
}

/// Percentile over a latency sample (nearest-rank); `None` when empty.
pub fn percentile(sorted_ms: &[i64], q: f64) -> Option<i64> {
    if sorted_ms.is_empty() {
        return None;
    }
    let rank = ((q * sorted_ms.len() as f64).ceil() as usize).clamp(1, sorted_ms.len());
    Some(sorted_ms[rank - 1])
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Detection, KeywordId};

    /// `consumed_ms` defaults to `pos_ms`; `rec_split` sets them apart.
    fn rec(tier: Tier, pos_ms: u64, emit_ns: u64) -> DetectionRecord {
        DetectionRecord {
            detection: Detection {
                keyword: KeywordId(0),
                confidence: 1.0,
                tier,
                audio_ts: MonotonicNs(0),
                emit_ts: MonotonicNs(emit_ns),
                audio_pos_ms: pos_ms,
                audio_consumed_ms: pos_ms,
                source: crate::types::SourceId(0),
            },
            intent: None,
            detect_latency_us: 0,
            leg: String::new(),
        }
    }

    fn occ(onset_ms: u64, end_ms: u64) -> Occurrence {
        Occurrence { phrase: "kw".into(), onset_ms, end_ms }
    }

    fn score<'a>(
        dets: &'a [DetectionRecord],
        occs: &'a [Occurrence],
        t0: Option<MonotonicNs>,
    ) -> Vec<TierScore> {
        let d: Vec<&DetectionRecord> = dets.iter().collect();
        let o: Vec<&Occurrence> = occs.iter().collect();
        score_phrase("kw", &d, &o, MatchWindow::default(), t0)
    }

    #[test]
    fn tp_miss_fa_accounting() {
        let dets = vec![
            rec(Tier::Fast, 2_300, 0),  // matches occurrence 1 (2000..2500)
            rec(Tier::Fast, 30_000, 0), // false alarm
            rec(Tier::Confirmed, 2_600, 0),
        ];
        let occs = vec![occ(2_000, 2_500), occ(60_000, 60_600)];
        let s = score(&dets, &occs, None);
        let fast = &s[0];
        assert_eq!((fast.true_positives, fast.misses, fast.false_alarms), (1, 1, 1));
        assert_eq!(fast.recall(), 0.5);
        assert_eq!(fast.precision(), 0.5);
        let conf = &s[1];
        assert_eq!((conf.true_positives, conf.misses, conf.false_alarms), (1, 1, 0));
    }

    #[test]
    fn one_detection_matches_at_most_one_occurrence() {
        // Two close occurrences, one detection in both windows → 1 TP + 1 miss.
        let dets = vec![rec(Tier::Fast, 3_000, 0)];
        let occs = vec![occ(2_000, 2_500), occ(2_600, 3_100)];
        let s = score(&dets, &occs, None);
        assert_eq!((s[0].true_positives, s[0].misses, s[0].false_alarms), (1, 1, 0));
    }

    #[test]
    fn window_bounds_are_inclusive() {
        // pre 250 → 1750 matches; post 1500 → 4000 matches; 4001 doesn't.
        for (pos, expect_tp) in [(1_750u64, 1u32), (4_000, 1), (1_749, 0), (4_001, 0)] {
            let dets = vec![rec(Tier::Fast, pos, 0)];
            let occs = vec![occ(2_000, 2_500)];
            let s = score(&dets, &occs, None);
            assert_eq!(s[0].true_positives, expect_tp, "pos={pos}");
        }
    }

    #[test]
    fn paced_latency_is_emit_minus_onset() {
        // Stream starts at t0=1s; occurrence onset 2s into content; emitted at
        // wall 1s + 2.35s → latency 350 ms.
        let t0 = MonotonicNs(1_000_000_000);
        let dets = vec![rec(Tier::Fast, 2_400, 3_350_000_000)];
        let occs = vec![occ(2_000, 2_500)];
        let s = score(&dets, &occs, Some(t0));
        assert_eq!(s[0].latency_ms, vec![350]);
    }

    #[test]
    fn emit_lag_is_audio_domain_and_needs_no_clock() {
        // Word ends at 2500 ms; engine surfaced the hit at 2900 ms of stream
        // position → it consumed 400 ms of audio past the word before firing.
        // No wall clock is supplied (t0 = None) — this is the whole point:
        // lossless speed=0 runs report it, so the laptop's run variance and the
        // box's oscillator drift are both irrelevant to the number.
        let dets = vec![rec(Tier::Fast, 2_900, 0)];
        let occs = vec![occ(2_000, 2_500)];
        let s = score(&dets, &occs, None);
        assert_eq!(s[0].emit_lag_ms, vec![400]);
        assert!(s[0].latency_ms.is_empty(), "no clock ⇒ no wall-clock latency");
    }

    #[test]
    fn emit_lag_is_negative_when_the_engine_fires_before_the_word_ends() {
        // Prefix-style firing: the hit surfaces 200 ms BEFORE the word finishes.
        // That is the outcome the parked prefix work is chasing, so it must be
        // representable — clamping to zero here would hide the win.
        let dets = vec![rec(Tier::Fast, 2_300, 0)];
        let occs = vec![occ(2_000, 2_500)];
        let s = score(&dets, &occs, None);
        assert_eq!(s[0].emit_lag_ms, vec![-200]);
    }

    #[test]
    fn consumed_lag_can_differ_from_emit_lag_and_that_is_the_point() {
        // A frame-scoring engine says "the evidence was at 2400 ms" while having
        // already consumed 2700 ms of audio to say it. emit_lag reads -100,
        // consumed_lag reads +200. Racing on emit_lag would credit this engine
        // with 300 ms it did not earn — exactly the D1b confound.
        let mut r = rec(Tier::Fast, 2_400, 0);
        r.detection.audio_consumed_ms = 2_700;
        let s = score(&[r], &[occ(2_000, 2_500)], None);
        assert_eq!(s[0].emit_lag_ms, vec![-100]);
        assert_eq!(s[0].consumed_lag_ms, vec![200]);
    }

    #[test]
    fn percentile_nearest_rank() {
        let v = vec![100, 200, 300, 400, 500];
        assert_eq!(percentile(&v, 0.5), Some(300));
        assert_eq!(percentile(&v, 0.95), Some(500));
        assert_eq!(percentile(&[], 0.5), None);
    }
}
