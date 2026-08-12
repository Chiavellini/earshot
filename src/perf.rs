//! OS-level determinism hooks — the "M5 OS tuning" seam (docs/OS-TUNING.md).
//!
//! Core pinning + SCHED_FIFO for the hot pipeline threads, applied per-thread
//! at spawn when `[perf] realtime = true`. Linux-only by design: the us-east
//! event box is a dedicated, tuned host (isolcpus/nohz_full per
//! docs/OS-TUNING.md); the macOS dev laptop and CI get a logged no-op.
//!
//! Failure policy: NEVER panic, NEVER refuse to run. A missing capability
//! (no CAP_SYS_NICE, un-tuned kernel, core index out of range) degrades to
//! the default scheduler with a single WARN — an event run on a half-tuned
//! box must still trade, just with more jitter. The preflight/os-tune-check
//! script is where hard validation belongs, not the hot path.

use serde::Deserialize;

/// `[perf]` config section. Config can only *enable* tuning on a prepared
/// box; defaults are all off so every existing config behaves identically.
#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PerfSection {
    /// Master switch: apply core pinning + SCHED_FIFO to hot threads.
    #[serde(default)]
    pub realtime: bool,
    /// Isolated-core assignments (see docs/OS-TUNING.md for the layout).
    /// Absent = that thread is not pinned. `core_source` pins the thread that
    /// drives the audio source (capture timestamps originate there).
    #[serde(default)]
    pub core_source: Option<usize>,
    #[serde(default)]
    pub core_preproc: Option<usize>,
    #[serde(default)]
    pub core_chunker: Option<usize>,
    #[serde(default)]
    pub core_detect: Option<usize>,
    /// Reserved for the executor runtime thread (wired with the dedicated
    /// executor runtime; the shared 2-worker runtime is deliberately NOT
    /// pinned — it also runs WS/timers/housekeeping).
    #[serde(default)]
    pub core_executor: Option<usize>,
    /// SCHED_FIFO priority (1–99) for hot threads when `realtime`. 80 leaves
    /// headroom below kernel threads that ship at 99.
    #[serde(default = "default_rt_priority")]
    pub rt_priority: u32,
    /// Busy-spin ring consumers (no park/unpark on the hot hops, ~100 ns
    /// reaction). Requires `realtime` AND pairwise-distinct hot cores —
    /// two spinning SCHED_FIFO threads on one core starve each other; config
    /// validation refuses that at load, never at fire time.
    #[serde(default)]
    pub spin_hot_rings: bool,
}

fn default_rt_priority() -> u32 {
    80
}

/// Must mirror the serde field defaults exactly: an absent `[perf]` section
/// deserializes via `Default::default()` (the section-level `#[serde(default)]`
/// in EventConfig), while an empty `[perf]` table goes through the per-field
/// serde defaults — the two must be indistinguishable (tested below).
impl Default for PerfSection {
    fn default() -> Self {
        PerfSection {
            realtime: false,
            core_source: None,
            core_preproc: None,
            core_chunker: None,
            core_detect: None,
            core_executor: None,
            rt_priority: default_rt_priority(),
            spin_hot_rings: false,
        }
    }
}

impl PerfSection {
    /// Priority to apply, or None when realtime is off.
    pub fn fifo_priority(&self) -> Option<u32> {
        self.realtime.then_some(self.rt_priority)
    }
}

/// Pin the calling thread to `core` and/or make it SCHED_FIFO at `priority`.
/// Both parts are independent and both degrade to a WARN on failure.
pub fn apply_to_current_thread(name: &str, core: Option<usize>, priority: Option<u32>) {
    #[cfg(target_os = "linux")]
    {
        if let Some(core) = core {
            // SAFETY: CPU_* are pure bit operations on a zeroed libc struct;
            // sched_setaffinity(0, ..) targets only the calling thread.
            unsafe {
                let mut set: libc::cpu_set_t = std::mem::zeroed();
                libc::CPU_ZERO(&mut set);
                libc::CPU_SET(core, &mut set);
                if libc::sched_setaffinity(0, std::mem::size_of::<libc::cpu_set_t>(), &set) != 0 {
                    tracing::warn!(
                        thread = name,
                        core,
                        errno = std::io::Error::last_os_error().raw_os_error(),
                        "core pin failed — running unpinned"
                    );
                } else {
                    tracing::info!(thread = name, core, "pinned");
                }
            }
        }
        if let Some(prio) = priority {
            let prio = prio.clamp(1, 99) as libc::c_int;
            let param = libc::sched_param {
                sched_priority: prio,
            };
            // SAFETY: plain syscall on the calling thread (pid 0 = self).
            let rc = unsafe { libc::sched_setscheduler(0, libc::SCHED_FIFO, &param) };
            if rc != 0 {
                tracing::warn!(
                    thread = name,
                    prio,
                    errno = std::io::Error::last_os_error().raw_os_error(),
                    "SCHED_FIFO failed (needs CAP_SYS_NICE or an rtprio ulimit) — default scheduler"
                );
            } else {
                tracing::info!(thread = name, prio, "SCHED_FIFO");
            }
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        if core.is_some() || priority.is_some() {
            tracing::debug!(
                thread = name,
                "perf tuning is Linux-only — no-op on this platform"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_all_off() {
        let p: PerfSection = toml::from_str("").unwrap();
        assert!(!p.realtime);
        assert!(p.core_detect.is_none());
        assert_eq!(p.rt_priority, 80);
        assert!(p.fifo_priority().is_none(), "no FIFO unless realtime");
    }

    #[test]
    fn absent_section_equals_empty_section() {
        // Absent [perf] uses Default::default(); empty [perf] uses the serde
        // field defaults. They must agree — a divergence here once shipped
        // rt_priority 0 to every config without a [perf] section.
        let empty: PerfSection = toml::from_str("").unwrap();
        let default = PerfSection::default();
        assert_eq!(format!("{empty:?}"), format!("{default:?}"));
    }

    #[test]
    fn realtime_enables_fifo_priority() {
        let p: PerfSection = toml::from_str("realtime = true\nrt_priority = 55").unwrap();
        assert_eq!(p.fifo_priority(), Some(55));
    }

    #[test]
    fn apply_never_panics_even_unprivileged() {
        // On Linux without CAP_SYS_NICE this exercises both WARN paths; on
        // macOS the no-op. Either way: must not panic — an event run on an
        // untuned box still trades.
        apply_to_current_thread("test", Some(0), Some(80));
        apply_to_current_thread("test", None, None);
    }
}
