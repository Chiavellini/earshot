//! The single detector construction point (spec §5.4: engine selection is
//! config, not code). Both wiring sites (`main.rs` replay + arm) call
//! `build_detector`; adding an engine means a new match arm here, never a
//! third wiring site.

use thiserror::Error;

use crate::config::{EngineKind, EventConfig};
use crate::detector::stub::EnergyStubDetector;
use crate::detector::Detector;

#[derive(Debug, Error)]
pub enum DetectorBuildError {
    #[error("detector engine {0:?} is not built yet (lands in M1 phase 3)")]
    NotImplemented(EngineKind),
    #[error("sherpa_kws: {0}")]
    SherpaKws(String),
}

/// Refuse-to-arm on any failure (P3: never start a session with a dead or
/// misconfigured detector).
pub fn build_detector(cfg: &EventConfig) -> Result<Box<dyn Detector>, DetectorBuildError> {
    match cfg.detector.engine {
        EngineKind::Stub => Ok(Box::new(EnergyStubDetector::for_standard_fixture())),
        EngineKind::SherpaKws => Err(DetectorBuildError::NotImplemented(EngineKind::SherpaKws)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config;

    fn example_cfg() -> EventConfig {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("configs/example-event.toml");
        config::load(&path).unwrap()
    }

    #[test]
    fn default_config_builds_stub() {
        let cfg = example_cfg();
        build_detector(&cfg).unwrap();
    }

    #[test]
    fn sherpa_engine_refuses_until_phase_3() {
        let mut cfg = example_cfg();
        cfg.detector.engine = EngineKind::SherpaKws;
        assert!(matches!(
            build_detector(&cfg),
            Err(DetectorBuildError::NotImplemented(EngineKind::SherpaKws))
        ));
    }
}
