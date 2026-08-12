//! Monotonic clock for all intra-pipeline timing (spec §5.1).
//!
//! Wall-clock (NTP-synced) is for logs and Kalshi timestamps only; every
//! latency measurement uses this raw monotonic source so NTP slew can never
//! distort an edge histogram.
//!
//! Platform note: Linux uses CLOCK_MONOTONIC_RAW. macOS uses CLOCK_UPTIME_RAW,
//! which stops ticking while the system sleeps (Linux RAW does not). Irrelevant
//! within a live session, but don't compare timestamps across a laptop sleep.

use serde::Serialize;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub struct MonotonicNs(pub u64);

#[cfg(target_os = "linux")]
const CLOCK_ID: libc::clockid_t = libc::CLOCK_MONOTONIC_RAW;
#[cfg(target_os = "macos")]
const CLOCK_ID: libc::clockid_t = libc::CLOCK_UPTIME_RAW;

impl MonotonicNs {
    #[inline]
    pub fn now() -> Self {
        let mut ts = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        // SAFETY: valid pointer to a timespec; clock id is a supported constant.
        let rc = unsafe { libc::clock_gettime(CLOCK_ID, &mut ts) };
        debug_assert_eq!(rc, 0, "clock_gettime failed");
        MonotonicNs(ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64)
    }

    /// Nanoseconds elapsed since `earlier`; 0 if `earlier` is not earlier.
    #[inline]
    pub fn saturating_since(self, earlier: MonotonicNs) -> u64 {
        self.0.saturating_sub(earlier.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn monotonic_under_tight_loop() {
        let mut prev = MonotonicNs::now();
        for _ in 0..100_000 {
            let now = MonotonicNs::now();
            assert!(now >= prev, "clock went backwards: {now:?} < {prev:?}");
            prev = now;
        }
    }

    #[test]
    fn saturating_since_reversed_args_is_zero() {
        let a = MonotonicNs(100);
        let b = MonotonicNs(200);
        assert_eq!(b.saturating_since(a), 100);
        assert_eq!(a.saturating_since(b), 0);
    }

    #[test]
    fn advances_over_sleep() {
        let a = MonotonicNs::now();
        std::thread::sleep(std::time::Duration::from_millis(5));
        let b = MonotonicNs::now();
        assert!(b.saturating_since(a) >= 4_000_000);
    }
}
