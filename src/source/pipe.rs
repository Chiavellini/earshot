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

/// Ceiling on the respawn budget, mirroring the Twilio reconnect cap in
/// `config::validate` — a venue that kills a leg more often than this needs a
/// different fix, not more retries.
pub const MAX_RESPAWN_BUDGET: u32 = 20;

pub struct PipeSource {
    cmd: Vec<String>,
    sample_rate_hz: u32,
    idle_timeout: Duration,
    source_id: SourceId,
    /// How many leg deaths this source survives by respawning the decoder
    /// instead of returning. 0 (default) = one-shot, the pre-2026-08-12
    /// behavior, byte-for-byte. Mirrors `TwilioWsSource::max_reconnects`.
    max_respawns: u32,
    /// Pause between a leg death and the next spawn. A sequence gap is usually
    /// the CDN a second or two behind itself; respawning instantly just burns
    /// the budget against the same bad edge.
    respawn_backoff: Duration,
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
        let (max_respawns, respawn_backoff) = respawn_policy_from_env();
        Ok(PipeSource {
            cmd,
            sample_rate_hz,
            idle_timeout: Duration::from_secs(30), // mirrors twilio_ws's dead-call guard
            source_id,
            max_respawns,
            respawn_backoff,
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

    #[doc(hidden)] // tests: set the budget directly instead of through the env
    pub fn with_respawn_budget(mut self, max_respawns: u32, backoff: Duration) -> Self {
        self.max_respawns = max_respawns.min(MAX_RESPAWN_BUDGET);
        self.respawn_backoff = backoff;
        self
    }

    fn frame_bytes(&self) -> usize {
        (self.sample_rate_hz * FRAME_MS / 1000) as usize * 2 // s16le
    }
}

/// Read the respawn posture from the environment.
///
/// WHY ENV AND NOT CONFIG. The natural home is
/// `SourceConfig::Pipe { max_respawns, respawn_backoff_ms }` plus two lines in
/// `main.rs::build_leg`, exactly the treatment `TwilioStream::max_reconnects`
/// gets. Both files belong to other nodes, so this adapter defaults its own
/// policy — the same shape as `twilio_ws::bind`, which reads
/// `EARSHOT_REDIAL_DIR` here rather than taking it as a parameter. The request
/// for the config-native form is filed in `architecture/claims.toml` under
/// `root.source_pipe_contract`.
///
/// Default is 0: a leg death stays terminal unless someone deliberately turned
/// respawn on, so replay/bake-off/probe callers behave exactly as before.
fn respawn_policy_from_env() -> (u32, Duration) {
    let budget = std::env::var("EARSHOT_PIPE_MAX_RESPAWNS")
        .ok()
        .and_then(|v| v.trim().parse::<u32>().ok())
        .unwrap_or(0);
    if budget > MAX_RESPAWN_BUDGET {
        tracing::warn!(
            requested = budget,
            cap = MAX_RESPAWN_BUDGET,
            "EARSHOT_PIPE_MAX_RESPAWNS above the cap — clamping"
        );
    }
    let backoff_ms = std::env::var("EARSHOT_PIPE_RESPAWN_BACKOFF_MS")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .unwrap_or(2_000);
    (
        budget.min(MAX_RESPAWN_BUDGET),
        Duration::from_millis(backoff_ms),
    )
}

/// Why one decoder attempt ended. Named so the respawn WARN can say it, and so
/// budget exhaustion can reproduce the pre-respawn return byte-for-byte.
enum AttemptEnd {
    /// No bytes for `idle_timeout` while the child was still alive. The COHR
    /// 2026-08-12 shape: an HLS sequence gap leaves ffmpeg/streamlink running
    /// but permanently silent.
    Idle(Vec<String>),
    /// The read itself failed.
    Io(std::io::Error),
    /// The decoder exited. NOT harmless mid-call: streamlink prints "Stream
    /// ended" and exits ~25 s into a CBRS call that is still running, and this
    /// branch used to return `Ok(EndOfStream)`, which fanout does not even
    /// count as a died leg (`pipeline::run_pipeline_fanout`).
    Eof,
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

        // Hoisted across respawns, exactly as twilio_ws hoists it across
        // re-accepts: dense, strictly increasing `seq` for the WHOLE run is a
        // layer obligation, and a leg that restarted its numbering would make
        // every downstream gap statistic a lie.
        let mut seq = 0u64;
        // Drop-WARN throttle state (Ford 2026-07-28): a stale-catch-up firehose
        // pushed ~24× realtime through here; the per-frame WARN below became
        // 99.7 % of a 111 MB log, competed for CPU mid-race, and truncated the
        // copy that held the detection record. One line per second, with the
        // count, says the same thing at 1/1000th the cost.
        let mut dropped_since_warn = 0u64;
        let mut last_drop_warn = std::time::Instant::now();
        let mut attempt: u32 = 0;

        loop {
            let frames_before = seq;
            let end = self
                .run_one_decoder(
                    &mut tx,
                    frame_bytes,
                    &mut seq,
                    &mut dropped_since_warn,
                    &mut last_drop_warn,
                )
                .await?;

            // Budget exhausted (0 = the pre-2026-08-12 one-shot): map each end
            // to its historical return, byte-for-byte, so a real death is still
            // as loud as it ever was.
            if attempt >= self.max_respawns {
                return match end {
                    AttemptEnd::Idle(tail) => Err(SourceError::Protocol(format!(
                        "pipe source idle past {:?} — dead webcast stream (decoder stderr tail: {:?})",
                        self.idle_timeout, tail
                    ))),
                    AttemptEnd::Io(e) => Err(SourceError::Io(e)),
                    AttemptEnd::Eof => {
                        tracing::info!(frames = seq, "pipe source: decoder ended");
                        Ok(SourceExit::EndOfStream)
                    }
                };
            }

            attempt += 1;
            let (reason, detail) = match &end {
                AttemptEnd::Idle(tail) => ("idle", Some(format!("{tail:?}"))),
                AttemptEnd::Io(e) => ("io", Some(e.to_string())),
                AttemptEnd::Eof => ("decoder_exit", None),
            };
            tracing::warn!(
                attempt,
                max_respawns = self.max_respawns,
                reason,
                detail,
                frames = seq,
                frames_this_attempt = seq - frames_before,
                backoff_ms = self.respawn_backoff.as_millis() as u64,
                "pipe leg died mid-stream — respawning decoder"
            );
            // The production argv is `/bin/sh -c "... $(cat $HOME/<lc>-stream.url) ..."`,
            // so re-running it re-reads the URL file and re-evaluates any
            // cache-buster in it. That is the whole "fresh URL on respawn"
            // mechanism; no extra plumbing exists or is needed.
            tokio::time::sleep(self.respawn_backoff).await;
        }
    }
}

impl PipeSource {
    /// One decoder lifetime: spawn, frame its stdout until it ends, clean up.
    /// Returns WHY it ended; the caller owns the respawn decision.
    async fn run_one_decoder(
        &self,
        tx: &mut RingSender<AudioFrame>,
        frame_bytes: usize,
        seq: &mut u64,
        dropped_since_warn: &mut u64,
        last_drop_warn: &mut std::time::Instant,
    ) -> Result<AttemptEnd, SourceError> {
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
        // deadlock the decoder) and keep the tail for the error message. Both
        // the task and the buffer are PER ATTEMPT: the task is detached and
        // never joined, so a respawn that reused them would mix one decoder's
        // diagnosis into the next one's.
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
                    *dropped_since_warn += 1;
                    if last_drop_warn.elapsed() >= std::time::Duration::from_secs(1) {
                        tracing::warn!(
                            seq = *seq - 1,
                            dropped = *dropped_since_warn,
                            "ring full: pipe frames dropped (throttled, count since last warn)"
                        );
                        *dropped_since_warn = 0;
                        *last_drop_warn = std::time::Instant::now();
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
                    return Ok(AttemptEnd::Idle(tail));
                }
                Ok(Err(e)) => {
                    let _ = child.kill().await;
                    return Ok(AttemptEnd::Io(e));
                }
                Ok(Ok(0)) => {
                    // EOF: flush any trailing partial frame (odd final byte of
                    // a torn s16 sample is dropped by chunks_exact).
                    let leftover = buf.len() & !1usize;
                    if leftover > 0 {
                        emit(&mut buf, seq, tx, leftover);
                    }
                    let status = child.wait().await.map_err(SourceError::Io)?;
                    tracing::info!(frames = *seq, %status, "pipe source: decoder exited");
                    return Ok(AttemptEnd::Eof);
                }
                Ok(Ok(n)) => n,
            };
            buf.extend_from_slice(&read_chunk[..n]);
            while buf.len() >= frame_bytes {
                emit(&mut buf, seq, tx, frame_bytes);
            }
        }
    }
}
