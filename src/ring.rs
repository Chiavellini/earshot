//! Bounded SPSC ring buffers for inter-stage handoff (spec §4).
//!
//! Contract: the producer never blocks. A full ring returns the value to the
//! caller, which counts the drop (P3: a stalled consumer must be loud, not a
//! deadlocked source). The consumer spins briefly, then parks — and the
//! producer *unparks it on push* (eventcount pattern), so the worst-case wake
//! is one `unpark` syscall (~µs), not a PARK_TIMEOUT slice. PARK_TIMEOUT
//! remains only as a missed-wakeup backstop and for the producer-side
//! backpressure park in `send_blocking` (a full interior ring means the
//! pipeline is already drowning — never the racing case).
//!
//! `set_spin(true)` (RT boxes, docs/OS-TUNING.md) makes `recv` a pure
//! busy-spin: no park, no wake syscall, ~100 ns reaction. Only safe when the
//! consumer thread owns an isolated core — two spinning SCHED_FIFO threads
//! sharing a core starve each other (config validation enforces distinct
//! cores).

use std::sync::atomic::{fence, AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use crate::clock::MonotonicNs;

/// Missed-wakeup backstop; the expected wake is the producer's `unpark`.
pub const PARK_TIMEOUT: Duration = Duration::from_micros(100);
const SPIN_ITERS: u32 = 64;
/// In spin mode, check for an abandoned producer every this many iterations
/// (the check reads an Arc counter — keep it off the tight loop).
const SPIN_ABANDON_CHECK: u32 = 4096;

pub struct RingFull<T>(pub T);

impl<T> std::fmt::Debug for RingFull<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("RingFull(..)")
    }
}

/// Producer↔consumer wake channel (eventcount-lite). The consumer publishes
/// its `Thread` handle once; `parked` + SeqCst fences implement the classic
/// Dekker handshake: either the consumer's re-check `pop` sees the push, or
/// the producer's `parked` load sees the flag and unparks. Either way no push
/// is slept through.
struct WakeState {
    consumer: OnceLock<std::thread::Thread>,
    parked: AtomicBool,
    /// Producer stamp taken just before `unpark` (ns); consumer reads it on
    /// wake to measure the actual push→wake cost (`ring_wake` edge).
    wake_req_ns: AtomicU64,
}

pub struct RingSender<T> {
    inner: rtrb::Producer<T>,
    drops: Arc<AtomicU64>,
    wake: Arc<WakeState>,
}

pub struct RingReceiver<T> {
    inner: rtrb::Consumer<T>,
    drops: Arc<AtomicU64>,
    wake: Arc<WakeState>,
    spin: bool,
    /// push→wake cost of the most recent recv that actually parked (ns).
    /// Consumers that own a recorder read this via `take_last_wake_ns`.
    last_wake_ns: Option<u64>,
}

pub fn ring<T>(capacity: usize) -> (RingSender<T>, RingReceiver<T>) {
    let (p, c) = rtrb::RingBuffer::new(capacity);
    let drops = Arc::new(AtomicU64::new(0));
    let wake = Arc::new(WakeState {
        consumer: OnceLock::new(),
        parked: AtomicBool::new(false),
        wake_req_ns: AtomicU64::new(0),
    });
    (
        RingSender { inner: p, drops: drops.clone(), wake: wake.clone() },
        RingReceiver { inner: c, drops, wake, spin: false, last_wake_ns: None },
    )
}

impl<T> RingSender<T> {
    /// Non-blocking. On a full ring the value is returned and the drop counter
    /// incremented; the caller decides whether to log/abort.
    pub fn send(&mut self, value: T) -> Result<(), RingFull<T>> {
        self.try_send(value).map_err(|e| {
            self.drops.fetch_add(1, Ordering::Relaxed);
            e
        })
    }

    /// Non-blocking, does NOT count a drop — for callers that retry (lossless
    /// offline replay). The counter must keep meaning "values actually lost".
    pub fn try_send(&mut self, value: T) -> Result<(), RingFull<T>> {
        match self.inner.push(value) {
            Ok(()) => {
                // Dekker handshake (see WakeState): the fence orders the ring
                // publish before the parked load, pairing with the consumer's
                // fence between its parked store and re-check pop.
                fence(Ordering::SeqCst);
                if self.wake.parked.load(Ordering::Relaxed) {
                    if let Some(t) = self.wake.consumer.get() {
                        self.wake.wake_req_ns.store(MonotonicNs::now().0, Ordering::Relaxed);
                        t.unpark();
                    }
                }
                Ok(())
            }
            Err(rtrb::PushError::Full(v)) => Err(RingFull(v)),
        }
    }

    /// Blocking send for interior pipeline stages: applies backpressure
    /// upstream instead of dropping. Only the ingress stage may drop — that
    /// keeps exactly one loud drop point per pipeline. Spins, then parks in
    /// PARK_TIMEOUT slices (no consumer→producer unpark: a full interior ring
    /// is the drowning case, not the racing case).
    pub fn send_blocking(&mut self, mut value: T) {
        loop {
            for _ in 0..SPIN_ITERS {
                match self.try_send(value) {
                    Ok(()) => return,
                    Err(RingFull(v)) => value = v,
                }
                std::hint::spin_loop();
            }
            std::thread::park_timeout(PARK_TIMEOUT);
        }
    }

    pub fn drop_count(&self) -> u64 {
        self.drops.load(Ordering::Relaxed)
    }

    /// Shared counter handle, readable after the sender moves into its thread.
    pub fn drops_handle(&self) -> Arc<AtomicU64> {
        self.drops.clone()
    }
}

impl<T> RingReceiver<T> {
    /// Busy-spin mode: `recv` never parks. RT boxes only — the consumer must
    /// own an isolated core (docs/OS-TUNING.md; config validation enforces
    /// distinct cores for spinning consumers).
    pub fn set_spin(&mut self, spin: bool) {
        self.spin = spin;
    }

    /// push→wake cost (ns) of the most recent `recv` that parked, if any.
    /// Cleared on read: each sample is recorded once (`ring_wake` edge).
    pub fn take_last_wake_ns(&mut self) -> Option<u64> {
        self.last_wake_ns.take()
    }

    /// Blocks until a value arrives: spin, then park until the producer's
    /// unpark (PARK_TIMEOUT backstop). In spin mode: pure busy-spin.
    /// Returns None once the sender is dropped and the ring is drained.
    pub fn recv(&mut self) -> Option<T> {
        if self.spin {
            let mut n = 0u32;
            loop {
                if let Ok(v) = self.inner.pop() {
                    return Some(v);
                }
                n = n.wrapping_add(1);
                if n % SPIN_ABANDON_CHECK == 0 && self.inner.is_abandoned() {
                    // Drain race: sender may have pushed then dropped.
                    return self.inner.pop().ok();
                }
                std::hint::spin_loop();
            }
        }
        loop {
            for _ in 0..SPIN_ITERS {
                if let Ok(v) = self.inner.pop() {
                    return Some(v);
                }
                std::hint::spin_loop();
            }
            if self.inner.is_abandoned() {
                // Drain race: sender may have pushed then dropped.
                return self.inner.pop().ok();
            }
            // Publish intent to park, then re-check (Dekker; see WakeState).
            self.wake.consumer.get_or_init(std::thread::current);
            self.wake.parked.store(true, Ordering::Relaxed);
            fence(Ordering::SeqCst);
            if let Ok(v) = self.inner.pop() {
                self.wake.parked.store(false, Ordering::Relaxed);
                return Some(v);
            }
            std::thread::park_timeout(PARK_TIMEOUT);
            self.wake.parked.store(false, Ordering::Relaxed);
            // If the producer initiated this wake, measure push→wake. A stale
            // stamp (timeout wake, no unpark) is filtered by re-zeroing.
            let req = self.wake.wake_req_ns.swap(0, Ordering::Relaxed);
            if req > 0 {
                self.last_wake_ns = Some(MonotonicNs::now().0.saturating_sub(req));
            }
        }
    }

    /// Non-blocking variant.
    pub fn try_recv(&mut self) -> Option<T> {
        self.inner.pop().ok()
    }

    pub fn drop_count(&self) -> u64 {
        self.drops.load(Ordering::Relaxed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_order_across_threads() {
        let (mut tx, mut rx) = ring::<u64>(8);
        let producer = std::thread::spawn(move || {
            for i in 0..10_000u64 {
                loop {
                    match tx.send(i) {
                        Ok(()) => break,
                        Err(RingFull(_)) => std::thread::yield_now(),
                    }
                }
            }
        });
        for expect in 0..10_000u64 {
            assert_eq!(rx.recv(), Some(expect));
        }
        producer.join().unwrap();
        assert_eq!(rx.recv(), None);
    }

    #[test]
    fn full_ring_returns_value_and_counts_drop() {
        let (mut tx, rx) = ring::<u32>(2);
        tx.send(1).unwrap();
        tx.send(2).unwrap();
        let RingFull(v) = tx.send(3).unwrap_err();
        assert_eq!(v, 3);
        assert_eq!(tx.drop_count(), 1);
        assert_eq!(rx.drop_count(), 1);
    }

    #[test]
    fn recv_returns_none_after_sender_drop() {
        let (mut tx, mut rx) = ring::<u32>(4);
        tx.send(7).unwrap();
        drop(tx);
        assert_eq!(rx.recv(), Some(7));
        assert_eq!(rx.recv(), None);
    }

    #[test]
    fn unpark_wakes_parked_consumer_quickly() {
        // An idle consumer must react to a push well before the 100 µs park
        // backstop, and a wake that went through park→unpark must yield a
        // ring_wake sample. The push CAN legitimately land in the consumer's
        // brief spin window between parks (then there's no park, no sample —
        // by design), so retry until a genuinely parked wake is observed.
        let mut sampled = None;
        for _ in 0..5 {
            let (mut tx, mut rx) = ring::<u32>(4);
            let consumer = std::thread::spawn(move || {
                let v = rx.recv();
                (v, rx.take_last_wake_ns())
            });
            // Let the consumer reach its park (parked ~99.9% of each 100 µs
            // cycle by the time 20 ms have passed).
            std::thread::sleep(Duration::from_millis(20));
            let t0 = std::time::Instant::now();
            tx.send(42).unwrap();
            let (v, wake_ns) = consumer.join().unwrap();
            let elapsed = t0.elapsed();
            assert_eq!(v, Some(42));
            assert!(
                elapsed < Duration::from_millis(10),
                "consumer took {elapsed:?} after push — unpark not working"
            );
            if wake_ns.is_some() {
                sampled = wake_ns;
                break;
            }
        }
        assert!(
            sampled.is_some(),
            "no parked recv yielded a ring_wake sample in 5 attempts — stamp path broken"
        );
    }

    #[test]
    fn spin_mode_receives_and_terminates() {
        let (mut tx, mut rx) = ring::<u32>(4);
        rx.set_spin(true);
        let consumer = std::thread::spawn(move || {
            let mut got = Vec::new();
            while let Some(v) = rx.recv() {
                got.push(v);
            }
            got
        });
        for i in 0..100 {
            loop {
                match tx.send(i) {
                    Ok(()) => break,
                    Err(RingFull(_)) => std::thread::yield_now(),
                }
            }
        }
        drop(tx); // spin recv must notice abandonment and return None
        let got = consumer.join().unwrap();
        assert_eq!(got, (0..100).collect::<Vec<_>>());
    }
}
