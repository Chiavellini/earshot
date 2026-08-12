//! Corpus ground-truth manifests (docs/BAKEOFF.md). One TOML per call,
//! committed to the repo; the audio it points at is gitignored. Read ONLY by
//! the bench scorer — never by the detector or config compiler (leakage).

use std::path::{Path, PathBuf};

use serde::Deserialize;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ManifestError {
    #[error("failed to read {path}: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("{path}: {err}")]
    Parse { err: toml::de::Error, path: String },
    #[error("{0}")]
    Invalid(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Split {
    /// Tuning surface: thresholds, prefixes, chunk_ms are chosen here.
    Dev,
    /// Run ONCE with the frozen config; reported numbers come from here.
    /// Pre-committed per call, never flipped after an engine has run.
    Holdout,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestFile {
    call: CallSection,
    #[serde(default)]
    occurrences: Vec<Occurrence>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CallSection {
    id: String,
    audio: String,
    split: Split,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Occurrence {
    /// Canonical phrase — must equal a config `KeywordConfig.phrase` to be
    /// scored (unknown phrases are ignored with a warning).
    pub phrase: String,
    pub onset_ms: u64,
    pub end_ms: u64,
}

#[derive(Debug)]
pub struct CallManifest {
    pub id: String,
    /// Absolute path; validated mono s16 at 8 kHz (telephony) or 16 kHz
    /// (wideband webcast channel) at load.
    pub audio: PathBuf,
    pub split: Split,
    pub duration_ms: u64,
    pub occurrences: Vec<Occurrence>,
}

/// Load every `manifests/*.toml` under the corpus root. Audio paths resolve
/// relative to the root. Loud failure on any invalid manifest.
pub fn load_dir(corpus_root: &Path) -> Result<Vec<CallManifest>, ManifestError> {
    let dir = corpus_root.join("manifests");
    let read_err = |source, p: &Path| ManifestError::Io {
        path: p.display().to_string(),
        source,
    };
    let mut out = Vec::new();
    let mut paths: Vec<PathBuf> = std::fs::read_dir(&dir)
        .map_err(|e| read_err(e, &dir))?
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e == "toml"))
        .collect();
    paths.sort();
    for path in paths {
        let text = std::fs::read_to_string(&path).map_err(|e| read_err(e, &path))?;
        let mf: ManifestFile = toml::from_str(&text).map_err(|err| ManifestError::Parse {
            err,
            path: path.display().to_string(),
        })?;
        out.push(validate(mf, corpus_root, &path)?);
    }
    if out.is_empty() {
        return Err(ManifestError::Invalid(format!(
            "no manifests in {}",
            dir.display()
        )));
    }
    Ok(out)
}

fn validate(mf: ManifestFile, root: &Path, src: &Path) -> Result<CallManifest, ManifestError> {
    let fail = |msg: String| Err(ManifestError::Invalid(format!("{}: {msg}", src.display())));
    let audio = root.join(&mf.call.audio);
    let reader = match hound::WavReader::open(&audio) {
        Ok(r) => r,
        Err(e) => return fail(format!("audio {}: {e}", audio.display())),
    };
    let spec = reader.spec();
    if spec.channels != 1
        || !(spec.sample_rate == 8000 || spec.sample_rate == 16000)
        || spec.bits_per_sample != 16
    {
        return fail(format!(
            "audio must be mono s16 at 8 kHz (telephony) or 16 kHz (wideband), got {} ch {} Hz {} bit",
            spec.channels, spec.sample_rate, spec.bits_per_sample
        ));
    }
    let duration_ms = reader.len() as u64 * 1000 / spec.sample_rate as u64;
    let mut prev_onset = 0u64;
    for occ in &mf.occurrences {
        if occ.end_ms <= occ.onset_ms {
            return fail(format!(
                "occurrence {:?} at {}: end ≤ onset",
                occ.phrase, occ.onset_ms
            ));
        }
        if occ.end_ms > duration_ms {
            return fail(format!(
                "occurrence {:?} at {} past audio end",
                occ.phrase, occ.onset_ms
            ));
        }
        if occ.onset_ms < prev_onset {
            return fail("occurrences must be sorted by onset_ms".into());
        }
        prev_onset = occ.onset_ms;
    }
    Ok(CallManifest {
        id: mf.call.id,
        audio,
        split: mf.call.split,
        duration_ms,
        occurrences: mf.occurrences,
    })
}
