//! Earshot — modular audio-signal runtime for Kalshi mention markets.
//! Stable node boundaries and contracts are indexed in `docs/NODES.md`.

pub mod bench;
pub mod capture_bakeoff;
pub mod clock;
pub mod config;
pub mod decision;
pub mod detector;
pub mod fixture;
pub mod fsio;
pub mod kalshi;
pub mod latprobe;
pub mod perf;
pub mod pipeline;
pub mod prepare;
pub mod ring;
pub mod risk;
pub mod source;
pub mod telemetry;
pub mod types;
pub mod xcorr;
