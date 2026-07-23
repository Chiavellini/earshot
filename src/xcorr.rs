//! Cross-correlation alignment of two mono PCM streams (spec §5.2 support).
//!
//! Two telephony legs of the *same* call connect at different wall-times, so
//! their frame sequence numbers are offset by a fixed lag. This recovers that
//! lag by normalized cross-correlation, and is used in exactly two places:
//! - `source::arbiter` TakeFirst mode — to align the legs before per-frame
//!   dedup, so "take whichever copy lands first" compares like audio with like.
//! - `capture_bakeoff` — to report the true inter-stream delta in ms on our
//!   actual bridges (the §3 bake-off deliverable).
//!
//! It is a pure function over `&[i16]`: no clock, no I/O, trivially testable.

/// Best integer lag (in samples) aligning `b` onto `a`, searched over
/// `[-max_lag, +max_lag]`, plus the normalized peak in `[-1.0, 1.0]`.
///
/// The returned lag `L` maximizes the correlation of `a[i]` with `b[i + L]`.
/// So a **positive** `L` means `b` is delayed relative to `a` (`b[k] == a[k - L]`):
/// `a`'s content shows up `L` samples later in `b`. The peak is the Pearson
/// correlation at the best lag: `1.0` for identical content, near `0.0` for
/// unrelated audio. Callers gate on the peak (e.g. `> 0.5`) before trusting the lag.
///
/// Naive `O(N · max_lag)`: fine for the one-time startup alignment (a couple
/// seconds of 8 kHz audio) and the offline bake-off. Not a hot-path routine.
pub fn best_lag(a: &[i16], b: &[i16], max_lag: usize) -> (i64, f64) {
    best_lag_in(a, b, -(max_lag as i64), max_lag as i64)
}

/// `best_lag` over an arbitrary lag range `[lo, hi]` (same sign convention).
/// Lets a caller refine around a coarse peak found on decimated audio
/// (`capture_bakeoff::wide_lag`) instead of sweeping the full window.
pub fn best_lag_in(a: &[i16], b: &[i16], lo: i64, hi: i64) -> (i64, f64) {
    if a.is_empty() || b.is_empty() {
        return (0, 0.0);
    }
    let bound = a.len().saturating_sub(1).min(b.len().saturating_sub(1)) as i64;
    let (lo, hi) = (lo.max(-bound), hi.min(bound));
    if lo > hi {
        return (0, 0.0);
    }

    let mut best = (lo, f64::NEG_INFINITY);
    // Correlate a[i] with b[i + lag] over the overlap.
    for lag in lo..=hi {
        let corr = correlation_at(a, b, lag);
        if corr > best.1 {
            best = (lag, corr);
        }
    }
    best
}

/// Pearson correlation of `a` and `b` over their overlapping region at `lag`
/// (see `best_lag` for the sign convention). Zero when the overlap is too
/// short or either window is silent (undefined correlation → not a match).
fn correlation_at(a: &[i16], b: &[i16], lag: i64) -> f64 {
    // Overlap indices in a: [a_lo, a_hi); the matching b index is `ia + lag`,
    // which must stay within `0..b.len()`.
    let (a_lo, a_hi) = if lag >= 0 {
        (0usize, a.len().min(b.len().saturating_sub(lag as usize)))
    } else {
        let off = (-lag) as usize;
        (off.min(a.len()), a.len().min(b.len() + off))
    };
    if a_hi <= a_lo {
        return 0.0;
    }
    let n = (a_hi - a_lo) as f64;
    if n < 64.0 {
        return 0.0; // too little overlap to be meaningful
    }

    let (mut sa, mut sb, mut saa, mut sbb, mut sab) = (0f64, 0f64, 0f64, 0f64, 0f64);
    for ia in a_lo..a_hi {
        let ib = (ia as i64 + lag) as usize;
        let x = a[ia] as f64;
        let y = b[ib] as f64;
        sa += x;
        sb += y;
        saa += x * x;
        sbb += y * y;
        sab += x * y;
    }
    let cov = sab - sa * sb / n;
    let va = saa - sa * sa / n;
    let vb = sbb - sb * sb / n;
    let denom = (va * vb).sqrt();
    if denom <= f64::EPSILON {
        0.0
    } else {
        cov / denom
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ramp(n: usize) -> Vec<i16> {
        (0..n).map(|i| (((i * 37) % 4001) as i16) - 2000).collect()
    }

    #[test]
    fn identical_streams_align_at_zero() {
        let a = ramp(4000);
        let (lag, peak) = best_lag(&a, &a, 500);
        assert_eq!(lag, 0);
        assert!(peak > 0.999, "peak {peak}");
    }

    #[test]
    fn recovers_positive_lag() {
        // b is a delayed by 120 samples: b[i] == a[i-120] → lag +120.
        let a = ramp(4000);
        let shift = 120usize;
        let mut b = vec![0i16; shift];
        b.extend_from_slice(&a[..a.len() - shift]);
        let (lag, peak) = best_lag(&a, &b, 500);
        assert_eq!(lag, shift as i64);
        assert!(peak > 0.999, "peak {peak}");
    }

    #[test]
    fn recovers_negative_lag() {
        // b leads a by 90 samples.
        let a = ramp(4000);
        let shift = 90usize;
        let b = a[shift..].to_vec();
        let (lag, peak) = best_lag(&a, &b, 500);
        assert_eq!(lag, -(shift as i64));
        assert!(peak > 0.999, "peak {peak}");
    }

    #[test]
    fn range_search_excluding_zero_recovers_lag() {
        // Same +120-sample shift, but searched in a window that excludes 0 —
        // the refine-around-coarse-peak use in capture_bakeoff::wide_lag.
        let a = ramp(4000);
        let shift = 120usize;
        let mut b = vec![0i16; shift];
        b.extend_from_slice(&a[..a.len() - shift]);
        let (lag, peak) = best_lag_in(&a, &b, 100, 200);
        assert_eq!(lag, shift as i64);
        assert!(peak > 0.999, "peak {peak}");
    }

    #[test]
    fn empty_range_is_no_match() {
        let a = ramp(1000);
        assert_eq!(best_lag_in(&a, &a, 50, 10), (0, 0.0));
    }

    #[test]
    fn unrelated_streams_have_low_peak() {
        let a = ramp(4000);
        let b: Vec<i16> = (0..4000).map(|i| (((i * 911) % 6000) as i16) - 3000).collect();
        let (_lag, peak) = best_lag(&a, &b, 500);
        assert!(peak < 0.5, "unrelated peak too high: {peak}");
    }
}
