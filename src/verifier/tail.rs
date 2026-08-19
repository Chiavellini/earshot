//! Fixed-length PCM ring kept by the detect thread: the last `tail_ms` of the
//! leg's audio, snapshotted (oldest → newest) when a detection asks to be
//! verified. Push is a memcpy of one chunk; snapshot allocates once.

use std::sync::Arc;

pub struct PcmTail {
    buf: Vec<i16>,
    head: usize,   // next write position
    filled: usize, // valid samples (≤ buf.len())
    rate_hz: u32,
}

impl PcmTail {
    pub fn new(tail_ms: u32, rate_hz: u32) -> Self {
        let n = (tail_ms as usize * rate_hz as usize) / 1000;
        PcmTail {
            buf: vec![0; n.max(1)],
            head: 0,
            filled: 0,
            rate_hz,
        }
    }

    pub fn push(&mut self, pcm: &[i16]) {
        let cap = self.buf.len();
        if pcm.len() >= cap {
            // only the newest `cap` samples matter
            self.buf.copy_from_slice(&pcm[pcm.len() - cap..]);
            self.head = 0;
            self.filled = cap;
            return;
        }
        let first = (cap - self.head).min(pcm.len());
        self.buf[self.head..self.head + first].copy_from_slice(&pcm[..first]);
        let rest = pcm.len() - first;
        if rest > 0 {
            self.buf[..rest].copy_from_slice(&pcm[first..]);
        }
        self.head = (self.head + pcm.len()) % cap;
        self.filled = (self.filled + pcm.len()).min(cap);
    }

    /// Oldest → newest copy of everything held.
    pub fn snapshot(&self) -> Arc<[i16]> {
        let cap = self.buf.len();
        let mut out = Vec::with_capacity(self.filled);
        if self.filled < cap {
            out.extend_from_slice(&self.buf[..self.filled]);
        } else {
            out.extend_from_slice(&self.buf[self.head..]);
            out.extend_from_slice(&self.buf[..self.head]);
        }
        Arc::from(out)
    }

    pub fn len_ms(&self) -> u32 {
        (self.filled as u64 * 1000 / self.rate_hz as u64) as u32
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wraps_and_snapshots_oldest_first() {
        let mut t = PcmTail::new(1, 8000); // 8 samples
        assert_eq!(t.buf.len(), 8);
        t.push(&[1, 2, 3]);
        assert_eq!(&*t.snapshot(), &[1, 2, 3]);
        assert_eq!(t.len_ms(), 0);
        t.push(&[4, 5, 6, 7, 8, 9]); // 9 total → keeps 2..=9
        assert_eq!(&*t.snapshot(), &[2, 3, 4, 5, 6, 7, 8, 9]);
        assert_eq!(t.len_ms(), 1);
        t.push(&[10; 20]); // bigger than the ring
        assert_eq!(&*t.snapshot(), &[10; 8]);
        t.push(&[11, 12]);
        assert_eq!(&*t.snapshot(), &[10, 10, 10, 10, 10, 10, 11, 12]);
    }
}
