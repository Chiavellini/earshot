//! Earshot — audio-signal latency trader for Kalshi mention markets.
//! M0: replay → pipeline → stub detection → decision intents + latency report.

pub mod bench;
pub mod capture_bakeoff;
pub mod clock;
pub mod config;
pub mod decision;
pub mod detector;
pub mod fixture;
pub mod kalshi;
pub mod latprobe;
pub mod pipeline;
pub mod ring;
pub mod risk;
pub mod source;
pub mod telemetry;
pub mod types;
pub mod xcorr;
