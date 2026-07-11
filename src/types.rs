//! Core pipeline types (spec §5.1, §5.4).

use std::sync::Arc;

use serde::Serialize;

use crate::clock::MonotonicNs;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub struct SourceId(pub u16);

/// Index into the compiled keyword table (config order).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub struct KeywordId(pub u16);

#[derive(Debug, Clone)]
pub struct AudioFrame {
    pub pcm: Arc<[i16]>,          // mono, native sample rate
    pub sample_rate_hz: u32,      // 8000 for PSTN, 16000/44100 elsewhere
    pub capture_ts: MonotonicNs,  // stamped the instant bytes arrive
    pub source: SourceId,
    pub seq: u64,
    /// Stamped by the preproc stage; MonotonicNs(0) until then.
    pub preproc_ts: MonotonicNs,
}

/// Detector input: fixed-duration accumulation of frames (spec §5.3 chunker).
#[derive(Debug, Clone)]
pub struct AudioChunk {
    pub pcm: Arc<[i16]>,
    pub sample_rate_hz: u32,
    pub first_capture_ts: MonotonicNs,
    pub last_capture_ts: MonotonicNs,
    pub first_preproc_ts: MonotonicNs,
    pub chunk_ts: MonotonicNs,    // stamped when the chunk is sealed
    pub source: SourceId,
    pub seq: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub enum Tier {
    Fast,
    Confirmed,
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct Detection {
    pub keyword: KeywordId,
    pub confidence: f32,
    pub tier: Tier,
    pub audio_ts: MonotonicNs,    // capture_ts of triggering audio
    pub emit_ts: MonotonicNs,
}

/// Output of the decision stage; the M2 executor consumes these from a ring.
#[derive(Debug, Clone, Serialize)]
pub struct DecisionIntent {
    pub keyword: KeywordId,
    pub market_ticker: String,
    pub max_price_cents: u8,
    pub tranche_contracts: u32,
    pub tier: Tier,
    pub audio_ts: MonotonicNs,
    pub decision_ts: MonotonicNs,
}
