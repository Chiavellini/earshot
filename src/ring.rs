//! Bounded SPSC ring buffers for inter-stage handoff (spec §4).
//!
//! Contract: the producer never blocks. A full ring returns the value to the
//! caller, which counts the drop (P3: a stalled consumer must be loud, not a
//! deadlocked source). The consumer spins briefly, then parks in short
//! timeouts — worst-case added latency per hop is PARK_TIMEOUT.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

/// Worst-case wake latency per hop; recorded here so latency histograms are
/// read with this constant in mind.
pub const PARK_TIMEOUT: Duration = Duration::from_micros(100);
const SPIN_ITERS: u32 = 64;

pub struct RingFull<T>(pub T);

impl<T> std::fmt::Debug for RingFull<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("RingFull(..)")
    }
}

pub struct RingSender<T> {
    inner: rtrb::Producer<T>,
    drops: Arc<AtomicU64>,
}

pub struct RingReceiver<T> {
    inner: rtrb::Consumer<T>,
    drops: Arc<AtomicU64>,
}

pub fn ring<T>(capacity: usize) -> (RingSender<T>, RingReceiver<T>) {
    let (p, c) = rtrb::RingBuffer::new(capacity);
    let drops = Arc::new(AtomicU64::new(0));
    (
        RingSender { inner: p, drops: drops.clone() },
        RingReceiver { inner: c, drops },
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
            Ok(()) => Ok(()),
            Err(rtrb::PushError::Full(v)) => Err(RingFull(v)),
        }
    }

    /// Blocking send for interior pipeline stages: applies backpressure
    /// upstream instead of dropping. Only the ingress stage may drop — that
    /// keeps exactly one loud drop point per pipeline. Spins, then parks in
    /// PARK_TIMEOUT slices.
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
    /// Blocks (spin, then park in PARK_TIMEOUT slices) until a value arrives.
    /// Returns None once the sender is dropped and the ring is drained.
    pub fn recv(&mut self) -> Option<T> {
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
            std::thread::park_timeout(PARK_TIMEOUT);
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
}
