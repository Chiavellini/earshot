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
    #[error("sherpa_kws: {0}")]
    SherpaKws(String),
}

/// Refuse-to-arm on any failure (P3: never start a session with a dead or
/// misconfigured detector).
pub fn build_detector(cfg: &EventConfig) -> Result<Box<dyn Detector>, DetectorBuildError> {
    match cfg.detector.engine {
        EngineKind::Stub => Ok(Box::new(EnergyStubDetector::for_standard_fixture())),
        EngineKind::SherpaKws => {
            // Config validation guarantees the section exists for this engine.
            let sk = cfg.detector.sherpa_kws.as_ref().expect("validated config");
            if cfg.pipeline.resample_to_hz != Some(16_000) {
                tracing::warn!(
                    "sherpa_kws without pipeline.resample_to_hz = 16000: only valid if every source is 16 kHz-native"
                );
            }
            Ok(Box::new(crate::detector::sherpa_kws::SherpaKwsDetector::new(sk, &cfg.keywords)?))
        }
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
    fn sherpa_engine_with_missing_model_dir_refuses_to_arm() {
        let mut cfg = example_cfg();
        cfg.detector.engine = EngineKind::SherpaKws;
        cfg.detector.sherpa_kws =
            Some(toml::from_str(r#"model_dir = "/nonexistent/model/dir""#).unwrap());
        assert!(matches!(build_detector(&cfg), Err(DetectorBuildError::SherpaKws(_))));
    }
}
