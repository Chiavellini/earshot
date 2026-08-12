//! `PipeSource` — webcast/generic subprocess adapter (spec §5.2 adapter #4).
//!
//! Spawns a decoder command (ffmpeg for HLS/DASH URLs; `yt-dlp | ffmpeg` for
//! YouTube; anything that can emit audio) and reads **raw s16le mono PCM** at a
//! declared sample rate from the child's stdout, emitting one `AudioFrame` per
//! `FRAME_MS` of samples, stamped at arrival. The subprocess owns all protocol,
//! demux, and codec work — this file owns only the layer's obligations:
//! arrival-stamped `capture_ts`, dense `seq`, the ring's drop policy, loud
//! failure on a dead stream, and never leaving an orphan child behind.
//!
//! Latency class is `Webcast`: this lane runs tens of seconds behind the phone
//! bridge (unmeasured — see docs/history/RUNBOOK.md) and exists for coverage (no-phone
//! issuers, failed number hunts), corpus, and slow markets — not for racing.

use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};

use super::{AudioSource, LatencyClass, SourceDescriptor, SourceError, SourceExit, SourceKind};
use crate::clock::MonotonicNs;
use crate::ring::RingSender;
use crate::types::{AudioFrame, SourceId};

pub const FRAME_MS: u32 = 20;

/// Webcast decode rate: wideband (an ASR-quality win over the 8 kHz phone
/// path); preproc handles resampling via `AudioFrame::sample_rate_hz`.
pub const HLS_SAMPLE_RATE_HZ: u32 = 16_000;

pub struct PipeSource {
    cmd: Vec<String>,
    sample_rate_hz: u32,
    idle_timeout: Duration,
    source_id: SourceId,
}

impl PipeSource {
    /// Generic form: `cmd` must write raw s16le mono PCM at `sample_rate_hz`
    /// to stdout. Errors cheaply on an empty command (fail before frames).
    pub fn new(
        cmd: Vec<String>,
        sample_rate_hz: u32,
        source_id: SourceId,
    ) -> Result<Self, SourceError> {
        if cmd.is_empty() {
            return Err(SourceError::Protocol("pipe source: empty command".into()));
        }
        if sample_rate_hz == 0 {
            return Err(SourceError::Protocol(
                "pipe source: zero sample rate".into(),
            ));
        }
        Ok(PipeSource {
            cmd,
            sample_rate_hz,
            idle_timeout: Duration::from_secs(30), // mirrors twilio_ws's dead-call guard
            source_id,
        })
    }

    /// `SourceConfig::Hls { url }` sugar: ffmpeg owns the protocol; we declare
    /// the output format explicitly so a drifting default can never silently
    /// change what "our PCM" means.
    pub fn hls(url: &str, source_id: SourceId) -> Result<Self, SourceError> {
        if url.is_empty() {
            return Err(SourceError::Protocol("hls source: empty url".into()));
        }
        let cmd = [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "warning",
            "-i",
            url,
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        PipeSource::new(cmd, HLS_SAMPLE_RATE_HZ, source_id)
    }

    #[doc(hidden)] // tests: shrink the dead-stream guard
    pub fn with_idle_timeout(mut self, d: Duration) -> Self {
        self.idle_timeout = d;
        self
    }

    fn frame_bytes(&self) -> usize {
        (self.sample_rate_hz * FRAME_MS / 1000) as usize * 2 // s16le
    }
}

#[async_trait]
impl AudioSource for PipeSource {
    fn descriptor(&self) -> SourceDescriptor {
        SourceDescriptor {
            kind: SourceKind::Hls,
            codec: "pcm_s16le",
            nominal_latency: LatencyClass::Webcast,
            clock_domain: "network",
        }
    }

    async fn run(
        self: Box<Self>,
        mut tx: RingSender<AudioFrame>,
    ) -> Result<SourceExit, SourceError> {
        let frame_bytes = self.frame_bytes();
        tracing::info!(cmd = ?self.cmd, rate = self.sample_rate_hz, "pipe source: spawning decoder");

        // kill_on_drop is the backstop; explicit kills below are the policy —
        // a decoder ffmpeg must never outlive the pipeline (orphan-process
        // lesson: RUNBOOK follow-up 5's cousin).
        let mut child = tokio::process::Command::new(&self.cmd[0])
            .args(&self.cmd[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(SourceError::Io)?;

        let mut stdout = child.stdout.take().expect("stdout piped");
        // Drain stderr concurrently (ffmpeg logs there; a full pipe would
        // deadlock the decoder) and keep the tail for the error message.
        let stderr = child.stderr.take().expect("stderr piped");
        let stderr_tail = Arc::new(std::sync::Mutex::new(std::collections::VecDeque::new()));
        let tail = stderr_tail.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                tracing::debug!(line = %line, "pipe decoder stderr");
                let mut t = tail.lock().unwrap();
                if t.len() >= 5 {
                    t.pop_front();
                }
                t.push_back(line);
            }
        });

        let mut buf: Vec<u8> = Vec::with_capacity(frame_bytes * 4);
        let mut read_chunk = vec![0u8; frame_bytes];
        let mut seq = 0u64;

        // Drop-WARN throttle state (Ford 2026-07-28): a stale-catch-up firehose
        // pushed ~24× realtime through here; the per-frame WARN below became
        // 99.7 % of a 111 MB log, competed for CPU mid-race, and truncated the
        // copy that held the detection record. One line per second, with the
        // count, says the same thing at 1/1000th the cost.
        let mut dropped_since_warn = 0u64;
        let mut last_drop_warn = std::time::Instant::now();

        let mut emit =
            |buf: &mut Vec<u8>, seq: &mut u64, tx: &mut RingSender<AudioFrame>, n: usize| {
                let bytes: Vec<u8> = buf.drain(..n).collect();
                let pcm: Arc<[i16]> = bytes
                    .chunks_exact(2)
                    .map(|b| i16::from_le_bytes([b[0], b[1]]))
                    .collect();
                let frame = AudioFrame {
                    pcm,
                    sample_rate_hz: self.sample_rate_hz,
                    capture_ts: MonotonicNs::now(),
                    source: self.source_id,
                    seq: *seq,
                    preproc_ts: MonotonicNs(0),
                };
                *seq += 1;
                // Live semantics: ingress is THE legitimate drop point — never
                // block the capture path on a stalled consumer.
                if tx.send(frame).is_err() {
                    dropped_since_warn += 1;
                    if last_drop_warn.elapsed() >= std::time::Duration::from_secs(1) {
                        tracing::warn!(
                            seq = *seq - 1,
                            dropped = dropped_since_warn,
                            "ring full: pipe frames dropped (throttled, count since last warn)"
                        );
                        dropped_since_warn = 0;
                        last_drop_warn = std::time::Instant::now();
                    }
                }
            };

        loop {
            let n = match tokio::time::timeout(self.idle_timeout, stdout.read(&mut read_chunk))
                .await
            {
                Err(_) => {
                    let _ = child.kill().await;
                    let tail: Vec<String> = stderr_tail.lock().unwrap().iter().cloned().collect();
                    return Err(SourceError::Protocol(format!(
                        "pipe source idle past {:?} — dead webcast stream (decoder stderr tail: {:?})",
                        self.idle_timeout, tail
                    )));
                }
                Ok(Err(e)) => {
                    let _ = child.kill().await;
                    return Err(SourceError::Io(e));
                }
                Ok(Ok(0)) => {
                    // EOF: flush any trailing partial frame (odd final byte of
                    // a torn s16 sample is dropped by chunks_exact).
                    let leftover = buf.len() & !1usize;
                    if leftover > 0 {
                        emit(&mut buf, &mut seq, &mut tx, leftover);
                    }
                    let status = child.wait().await.map_err(SourceError::Io)?;
                    tracing::info!(frames = seq, %status, "pipe source: decoder ended");
                    return Ok(SourceExit::EndOfStream);
                }
                Ok(Ok(n)) => n,
            };
            buf.extend_from_slice(&read_chunk[..n]);
            while buf.len() >= frame_bytes {
                emit(&mut buf, &mut seq, &mut tx, frame_bytes);
            }
        }
    }
}
