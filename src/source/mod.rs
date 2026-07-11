//! `AudioSource` — the adapter boundary (spec §5.2, P1).
//!
//! Nothing outside this layer may know where audio came from. New sources
//! (SIP dial-in, HLS webcast, local capture) implement the trait and add a
//! `SourceConfig` variant; the pipeline is unchanged.

pub mod replay;

use async_trait::async_trait;

use crate::ring::RingSender;
use crate::types::AudioFrame;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceKind {
    Replay,
    Sip,
    Hls,
    LocalCapture,
}

/// Coarse expectation for spec §3 stage-1/2 accounting, not a measurement.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LatencyClass {
    Synthetic, // replay: no real-world ingress latency
    Telephony, // ~tens of ms transit
    Webcast,   // ~tens of seconds behind
}

#[derive(Debug, Clone)]
pub struct SourceDescriptor {
    pub kind: SourceKind,
    pub codec: &'static str,
    pub nominal_latency: LatencyClass,
    /// Which clock the frames' pacing derives from (file timeline vs network).
    pub clock_domain: &'static str,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceExit {
    EndOfStream,
    Cancelled,
}

#[derive(Debug, thiserror::Error)]
pub enum SourceError {
    #[error("audio source I/O: {0}")]
    Io(#[from] std::io::Error),
    #[error("wav decode: {0}")]
    Wav(#[from] hound::Error),
}

#[async_trait]
pub trait AudioSource: Send {
    fn descriptor(&self) -> SourceDescriptor;
    async fn run(
        self: Box<Self>,
        tx: RingSender<AudioFrame>,
    ) -> Result<SourceExit, SourceError>;
}
