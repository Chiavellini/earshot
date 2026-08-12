//! `SourceArbiter` — dual dial-in fan-in (spec §5.2; the arbiter foreshadowed
//! in `docs/layers/source.md`).
//!
//! Two carriers dial the *same* earnings call; the arbiter runs both legs and
//! forwards one clean `AudioFrame` stream downstream. Taking the min of two
//! independent latency draws beats either vendor alone, and a dead leg no
//! longer kills the event — the highest-ROI item on the ingress leg.
//!
//! **The arbiter is itself an `AudioSource`.** It wraps N inner sources, so
//! `run_pipeline` and the `AudioFrame` contract are untouched: from the
//! pipeline's side there is still exactly one source producing into one ring.
//!
//! Each inner runs as its own task feeding a private ring; the arbiter drains
//! those rings and re-emits under a dense arbiter-local `seq` (so downstream
//! `seq == 0` / gap logic still holds). Each forwarded frame keeps its own
//! `capture_ts` — the true arrival time on the winning leg, which is exactly
//! the quantity being minimized. Ingress drop policy is unchanged: a full
//! downstream ring is a counted, logged drop, never backpressure.

use std::time::Duration;

use async_trait::async_trait;
use tokio::time::Instant;

use super::{AudioSource, LatencyClass, SourceDescriptor, SourceError, SourceExit, SourceKind};
use crate::ring::{self, RingReceiver, RingSender};
use crate::types::{AudioFrame, SourceId};
use crate::xcorr;

/// Per-inner ring capacity — matches the pipeline's `FRAME_RING_CAP`.
const LEG_RING_CAP: usize = 256;
/// Idle poll granularity. rtrb rings have no async waker, so the drain loop
/// polls; when nothing is available we park this long. 200 µs is on the order
/// of the pipeline's `ring::PARK_TIMEOUT` and negligible against the 30–150 ms
/// budget this whole leg optimizes.
const POLL_IDLE: Duration = Duration::from_micros(200);
/// Minimum normalized cross-correlation peak to trust leg alignment in
/// TakeFirst mode; below this the legs aren't the same audio → fail safe to
/// failover.
const ALIGN_MIN_PEAK: f64 = 0.5;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArbiterMode {
    /// Forward the primary; on stall/death switch to a live standby. Robust,
    /// always-defensible; buys the failover half of "min of two draws".
    Failover,
    /// Align the legs by cross-correlation, then forward whichever copy of each
    /// audio position lands first (dedup the later). The literal latency min.
    /// Falls back to `Failover` if the legs don't correlate.
    TakeFirst,
}

#[derive(Debug, Clone, Copy)]
pub struct ArbiterParams {
    pub mode: ArbiterMode,
    /// Failover: how long without a primary frame before switching legs.
    pub switch: Duration,
    /// TakeFirst: how much audio to buffer per leg before estimating the lag.
    pub align: Duration,
}

impl Default for ArbiterParams {
    fn default() -> Self {
        ArbiterParams {
            mode: ArbiterMode::Failover,
            switch: Duration::from_millis(200),
            align: Duration::from_secs(2),
        }
    }
}

pub struct SourceArbiter {
    inners: Vec<Box<dyn AudioSource>>,
    params: ArbiterParams,
    source_id: SourceId,
}

impl SourceArbiter {
    /// Requires ≥ 2 inners (a single-source config never reaches the arbiter —
    /// `build_source` keeps it on the direct path).
    pub fn new(
        inners: Vec<Box<dyn AudioSource>>,
        params: ArbiterParams,
        source_id: SourceId,
    ) -> Result<Self, SourceError> {
        if inners.len() < 2 {
            return Err(SourceError::Protocol(
                "SourceArbiter needs at least 2 inner sources".into(),
            ));
        }
        Ok(SourceArbiter {
            inners,
            params,
            source_id,
        })
    }
}

/// One inner source's task + its private receiver.
struct Leg {
    idx: usize,
    rx: RingReceiver<AudioFrame>,
    handle: tokio::task::JoinHandle<Result<SourceExit, SourceError>>,
}

fn forward_frame(
    tx: &mut RingSender<AudioFrame>,
    mut frame: AudioFrame,
    out_seq: &mut u64,
    source_id: SourceId,
) {
    frame.source = source_id;
    frame.seq = *out_seq;
    *out_seq += 1;
    if tx.send(frame).is_err() {
        tracing::warn!(seq = *out_seq - 1, "arbiter: ring full, frame dropped");
    }
}

/// Await the (already-finished) leg tasks and fold their exits into one:
/// `EndOfStream` if anything was forwarded or any leg ended cleanly; otherwise
/// surface the first error (every leg failed before producing a frame).
async fn collect_exit(legs: Vec<Leg>, forwarded_any: bool) -> Result<SourceExit, SourceError> {
    let mut first_err = None;
    let mut any_eos = false;
    for leg in legs {
        match leg.handle.await {
            Ok(Ok(SourceExit::EndOfStream)) => any_eos = true,
            Ok(Ok(SourceExit::Cancelled)) => {}
            Ok(Err(e)) => {
                tracing::warn!(leg = leg.idx, error = %e, "arbiter: leg ended with error");
                if first_err.is_none() {
                    first_err = Some(e);
                }
            }
            Err(join) => tracing::warn!(leg = leg.idx, %join, "arbiter: leg task join error"),
        }
    }
    if forwarded_any || any_eos {
        Ok(SourceExit::EndOfStream)
    } else {
        Err(first_err.unwrap_or_else(|| SourceError::Protocol("all arbiter legs failed".into())))
    }
}

#[async_trait]
impl AudioSource for SourceArbiter {
    fn descriptor(&self) -> SourceDescriptor {
        SourceDescriptor {
            kind: SourceKind::Sip,
            codec: self.inners[0].descriptor().codec,
            nominal_latency: LatencyClass::Telephony,
            clock_domain: "network",
        }
    }

    async fn run(self: Box<Self>, tx: RingSender<AudioFrame>) -> Result<SourceExit, SourceError> {
        let SourceArbiter {
            inners,
            params,
            source_id,
        } = *self;
        tracing::info!(legs = inners.len(), mode = ?params.mode, "arbiter: starting dual dial-in");

        let mut legs = Vec::with_capacity(inners.len());
        for (idx, inner) in inners.into_iter().enumerate() {
            let (leg_tx, leg_rx) = ring::ring::<AudioFrame>(LEG_RING_CAP);
            let handle = tokio::spawn(async move { inner.run(leg_tx).await });
            legs.push(Leg {
                idx,
                rx: leg_rx,
                handle,
            });
        }

        match params.mode {
            ArbiterMode::Failover => run_failover(legs, tx, params.switch, source_id, 0).await,
            ArbiterMode::TakeFirst => run_take_first(legs, tx, params, source_id).await,
        }
    }
}

/// Forward the active leg; drain (and discard) standbys so a switch resumes at
/// the live position. Switch when the active leg has produced nothing for
/// `switch`, or has finished, and another leg is currently live.
async fn run_failover(
    mut legs: Vec<Leg>,
    mut tx: RingSender<AudioFrame>,
    switch: Duration,
    source_id: SourceId,
    start_out_seq: u64,
) -> Result<SourceExit, SourceError> {
    let n = legs.len();
    let mut out_seq = start_out_seq;
    let mut forwarded_any = start_out_seq > 0;
    let mut active = 0usize;
    let now = Instant::now();
    let mut last_frame = vec![now; n]; // last time each leg produced a frame

    loop {
        let mut progressed = false;
        for i in 0..n {
            while let Some(frame) = legs[i].rx.try_recv() {
                progressed = true;
                last_frame[i] = Instant::now();
                if legs[i].idx == active {
                    forward_frame(&mut tx, frame, &mut out_seq, source_id);
                    forwarded_any = true;
                }
                // standby frames: discard, keeping the standby's ring fresh.
            }
        }

        // Consider switching. Active is unhealthy if its task finished or it
        // has gone quiet past `switch`.
        let active_finished = legs[active].handle.is_finished();
        let active_quiet = last_frame[active].elapsed() >= switch;
        if active_finished || active_quiet {
            // Pick the liveliest *other* leg still within the switch window.
            let candidate = (0..n)
                .filter(|&i| i != active && !legs[i].handle.is_finished())
                .filter(|&i| last_frame[i].elapsed() < switch)
                .max_by_key(|&i| last_frame[i]);
            if let Some(next) = candidate {
                tracing::warn!(
                    from = active,
                    to = next,
                    reason = if active_finished {
                        "finished"
                    } else {
                        "stalled"
                    },
                    "arbiter: failover switch"
                );
                active = next;
                last_frame[active] = Instant::now();
            }
        }

        // Terminate only after every task has ended and a full drain sweep
        // pulled nothing — guarantees no in-flight frame is lost.
        if !progressed && legs.iter().all(|l| l.handle.is_finished()) {
            break;
        }
        if progressed {
            tokio::task::yield_now().await;
        } else {
            tokio::time::sleep(POLL_IDLE).await;
        }
    }

    collect_exit(legs, forwarded_any).await
}

/// Align the legs once at startup, then forward the first-arriving copy of each
/// audio position. On weak correlation, fall back to failover.
async fn run_take_first(
    mut legs: Vec<Leg>,
    mut tx: RingSender<AudioFrame>,
    params: ArbiterParams,
    source_id: SourceId,
) -> Result<SourceExit, SourceError> {
    let n = legs.len();

    // Phase A — buffer a slice of each leg to estimate the inter-leg lag.
    let mut buf: Vec<Vec<AudioFrame>> = (0..n).map(|_| Vec::new()).collect();
    let deadline = Instant::now() + params.align;
    loop {
        let mut progressed = false;
        for i in 0..n {
            while let Some(frame) = legs[i].rx.try_recv() {
                progressed = true;
                buf[i].push(frame);
            }
        }
        let all_finished = legs.iter().all(|l| l.handle.is_finished());
        if Instant::now() >= deadline || (all_finished && !progressed) {
            break;
        }
        if progressed {
            tokio::task::yield_now().await;
        } else {
            tokio::time::sleep(POLL_IDLE).await;
        }
    }

    // Reference leg 0 must have audio; each other leg aligns to it.
    let spf = buf[0].first().map(|f| f.pcm.len().max(1)).unwrap_or(160);
    let ref_pcm = concat_pcm(&buf[0]);
    let mut usable = vec![false; n];
    let mut first_seq = vec![0u64; n];
    let mut lag_frames = vec![0i64; n];
    let mut aligned_ok = !ref_pcm.is_empty();
    if aligned_ok {
        usable[0] = true;
        first_seq[0] = buf[0][0].seq;
        let max_lag = ref_pcm.len() / 2;
        for j in 1..n {
            let jpcm = concat_pcm(&buf[j]);
            if jpcm.is_empty() {
                continue;
            }
            let (lag, peak) = xcorr::best_lag(&ref_pcm, &jpcm, max_lag);
            if peak < ALIGN_MIN_PEAK {
                tracing::warn!(
                    leg = j,
                    peak,
                    "arbiter: leg failed to align — TakeFirst degraded"
                );
                aligned_ok = false;
                break;
            }
            usable[j] = true;
            first_seq[j] = buf[j][0].seq;
            // lag is in samples; a positive lag means leg j is delayed vs ref.
            lag_frames[j] = (lag as f64 / spf as f64).round() as i64;
            tracing::info!(
                leg = j,
                lag_frames = lag_frames[j],
                peak,
                "arbiter: leg aligned"
            );
        }
    }

    if !aligned_ok {
        // Fall back to failover for the whole call. Flush the reference leg's
        // buffered frames first so no audio is lost, then hand off.
        tracing::warn!("arbiter: TakeFirst alignment failed — falling back to Failover");
        let mut out_seq = 0u64;
        for frame in std::mem::take(&mut buf[0]) {
            forward_frame(&mut tx, frame, &mut out_seq, source_id);
        }
        return run_failover(legs, tx, params.switch, source_id, out_seq).await;
    }

    // refpos maps a leg-local seq onto the common reference timeline.
    let refpos =
        |leg: usize, seq: u64| -> i64 { (seq as i64 - first_seq[leg] as i64) - lag_frames[leg] };

    // Flush Phase A: forward each distinct audio position once, in order.
    let mut out_seq = 0u64;
    let mut pending: std::collections::BTreeMap<i64, AudioFrame> =
        std::collections::BTreeMap::new();
    for (leg, frames) in buf.into_iter().enumerate() {
        if !usable[leg] {
            continue;
        }
        for frame in frames {
            pending.entry(refpos(leg, frame.seq)).or_insert(frame);
        }
    }
    let mut watermark = i64::MIN;
    for (pos, frame) in pending {
        forward_frame(&mut tx, frame, &mut out_seq, source_id);
        watermark = pos;
    }
    let mut forwarded_any = out_seq > 0;

    // Phase B — steady state: first arrival of each position beyond the
    // watermark wins; later duplicates are dropped.
    loop {
        let mut progressed = false;
        for i in 0..n {
            while let Some(frame) = legs[i].rx.try_recv() {
                progressed = true;
                if !usable[i] {
                    continue; // unaligned leg: cannot place on the timeline
                }
                let pos = refpos(i, frame.seq);
                if pos > watermark {
                    watermark = pos;
                    forward_frame(&mut tx, frame, &mut out_seq, source_id);
                    forwarded_any = true;
                }
            }
        }
        if !progressed && legs.iter().all(|l| l.handle.is_finished()) {
            break;
        }
        if progressed {
            tokio::task::yield_now().await;
        } else {
            tokio::time::sleep(POLL_IDLE).await;
        }
    }

    collect_exit(legs, forwarded_any).await
}

fn concat_pcm(frames: &[AudioFrame]) -> Vec<i16> {
    let mut out = Vec::with_capacity(frames.iter().map(|f| f.pcm.len()).sum());
    for f in frames {
        out.extend_from_slice(&f.pcm);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::MonotonicNs;
    use crate::ring::RingSender;
    use std::sync::Arc;

    /// Test source that emits a fixed PCM script, one frame per `frame_gap`,
    /// after an initial `start_delay`. Lets us inject latency skew and stalls.
    struct ScriptedSource {
        frames: Vec<Vec<i16>>,
        start_delay: Duration,
        frame_gap: Duration,
        source_id: SourceId,
        /// Stop emitting after this many frames (simulate a dropped call).
        cut_after: Option<usize>,
    }

    #[async_trait]
    impl AudioSource for ScriptedSource {
        fn descriptor(&self) -> SourceDescriptor {
            SourceDescriptor {
                kind: SourceKind::Sip,
                codec: "pcm_s16le",
                nominal_latency: LatencyClass::Telephony,
                clock_domain: "network",
            }
        }
        async fn run(
            self: Box<Self>,
            mut tx: RingSender<AudioFrame>,
        ) -> Result<SourceExit, SourceError> {
            tokio::time::sleep(self.start_delay).await;
            let limit = self.cut_after.unwrap_or(self.frames.len());
            for (n, pcm) in self.frames.iter().enumerate() {
                if n >= limit {
                    break;
                }
                let frame = AudioFrame {
                    pcm: Arc::from(pcm.as_slice()),
                    sample_rate_hz: 8000,
                    capture_ts: MonotonicNs::now(),
                    source: self.source_id,
                    seq: n as u64,
                    preproc_ts: MonotonicNs(0),
                };
                let _ = tx.send(frame);
                tokio::time::sleep(self.frame_gap).await;
            }
            Ok(SourceExit::EndOfStream)
        }
    }

    // Speech-like: decorrelated samples so cross-correlation has a sharp peak
    // at the true lag (a smooth ramp would correlate ~1.0 at many offsets).
    fn script(n: usize) -> Vec<Vec<i16>> {
        (0..n)
            .map(|i| {
                (0..160)
                    .map(|s| {
                        let g = (i * 160 + s) as u64;
                        let h = g
                            .wrapping_mul(6364136223846793005)
                            .wrapping_add(1442695040888963407);
                        (h >> 33) as i16
                    })
                    .collect()
            })
            .collect()
    }

    fn drain(arb: SourceArbiter) -> Vec<AudioFrame> {
        let (tx, mut rx) = ring::ring::<AudioFrame>(4096);
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let consumer = std::thread::spawn(move || {
            let mut v = Vec::new();
            while let Some(f) = rx.recv() {
                v.push(f);
            }
            v
        });
        rt.block_on(Box::new(arb).run(tx)).unwrap();
        consumer.join().unwrap()
    }

    #[test]
    fn take_first_dedups_and_keeps_dense_seq() {
        // Two legs replay identical audio; leg B lags by a fixed start delay.
        // Every audio position must appear exactly once, seq dense from 0.
        let frames = script(40);
        let a = Box::new(ScriptedSource {
            frames: frames.clone(),
            start_delay: Duration::from_millis(0),
            frame_gap: Duration::from_millis(5),
            source_id: SourceId(1),
            cut_after: None,
        });
        let b = Box::new(ScriptedSource {
            frames: frames.clone(),
            start_delay: Duration::from_millis(30),
            frame_gap: Duration::from_millis(5),
            source_id: SourceId(2),
            cut_after: None,
        });
        let params = ArbiterParams {
            mode: ArbiterMode::TakeFirst,
            switch: Duration::from_millis(50),
            align: Duration::from_millis(80),
        };
        let arb = SourceArbiter::new(vec![a, b], params, SourceId(9)).unwrap();
        let out = drain(arb);

        assert_eq!(out.len(), 40, "each of 40 positions forwarded exactly once");
        for (i, f) in out.iter().enumerate() {
            assert_eq!(f.seq, i as u64, "dense arbiter seq");
            assert_eq!(f.source, SourceId(9), "re-stamped source id");
        }
    }

    #[test]
    fn failover_switches_when_primary_dies() {
        // Primary cuts out after 10 frames; standby carries the rest. Output
        // stays dense and covers well past the primary's cut.
        let frames = script(40);
        let primary = Box::new(ScriptedSource {
            frames: frames.clone(),
            start_delay: Duration::from_millis(0),
            frame_gap: Duration::from_millis(5),
            source_id: SourceId(1),
            cut_after: Some(10),
        });
        let standby = Box::new(ScriptedSource {
            frames: frames.clone(),
            start_delay: Duration::from_millis(2),
            frame_gap: Duration::from_millis(5),
            source_id: SourceId(2),
            cut_after: None,
        });
        let params = ArbiterParams {
            mode: ArbiterMode::Failover,
            switch: Duration::from_millis(30),
            align: Duration::from_millis(50),
        };
        let arb = SourceArbiter::new(vec![primary, standby], params, SourceId(9)).unwrap();
        let out = drain(arb);

        assert!(
            out.len() >= 30,
            "standby should carry past the primary cut, got {}",
            out.len()
        );
        for (i, f) in out.iter().enumerate() {
            assert_eq!(f.seq, i as u64, "dense arbiter seq across the switch");
        }
    }

    #[test]
    fn rejects_single_source() {
        let a = Box::new(ScriptedSource {
            frames: script(1),
            start_delay: Duration::from_millis(0),
            frame_gap: Duration::from_millis(1),
            source_id: SourceId(1),
            cut_after: None,
        });
        assert!(SourceArbiter::new(vec![a], ArbiterParams::default(), SourceId(0)).is_err());
    }
}
