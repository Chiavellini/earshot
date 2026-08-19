//! Model-directory discovery shared by the sherpa-backed engines.
//!
//! sherpa's C++ `exit(-1)`s on a missing file / bad `modeling_unit` INSIDE the
//! FFI call, which would take the whole arm process down with no Rust-side
//! error. Every path is therefore resolved and checked here first; the engine
//! refuses to build with a message naming the directory, and sherpa only ever
//! sees files that exist.

use std::path::{Path, PathBuf};

use super::kws_compile::TokenTable;

/// Which sherpa online model family a directory holds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OnlineModelFiles {
    /// encoder / decoder / joiner ONNX (zipformer or NeMo cache-aware transducer)
    Transducer {
        encoder: PathBuf,
        decoder: PathBuf,
        joiner: PathBuf,
    },
    /// single `ctc-*.onnx` (icefall streaming zipformer2 CTC export)
    Zipformer2Ctc { model: PathBuf },
    /// single `model*.onnx` (NeMo cache-aware CTC)
    NemoCtc { model: PathBuf },
}

impl OnlineModelFiles {
    pub fn kind(&self) -> &'static str {
        match self {
            OnlineModelFiles::Transducer { .. } => "transducer",
            OnlineModelFiles::Zipformer2Ctc { .. } => "zipformer2_ctc",
            OnlineModelFiles::NemoCtc { .. } => "nemo_ctc",
        }
    }
    pub fn is_transducer(&self) -> bool {
        matches!(self, OnlineModelFiles::Transducer { .. })
    }
}

#[derive(Debug, Clone)]
pub struct DiscoveredOnline {
    pub files: OnlineModelFiles,
    pub tokens: PathBuf,
    pub bpe_model: Option<PathBuf>,
    pub bpe_vocab: Option<PathBuf>,
    /// tokens.txt carries uppercase letters (LibriSpeech/GigaSpeech BPE) — the
    /// hotwords buffer must be rendered in the vocabulary's case.
    pub uppercase_vocab: bool,
}

/// `stem*.onnx`, int8 or not. Pure over a file list so it is unit-testable
/// without a model on disk; `find_model` is the fs-backed wrapper.
pub fn pick_model(names: &[String], stem: &str, int8: bool) -> Option<String> {
    let mut hits: Vec<&String> = names
        .iter()
        .filter(|n| n.starts_with(stem) && n.ends_with(".onnx") && (n.contains(".int8.") == int8))
        .collect();
    hits.sort();
    hits.first().map(|s| s.to_string())
}

pub fn list_dir(dir: &Path) -> Result<Vec<String>, String> {
    let rd = std::fs::read_dir(dir).map_err(|e| format!("read {}: {e}", dir.display()))?;
    Ok(rd
        .flatten()
        .map(|f| f.file_name().to_string_lossy().into_owned())
        .collect())
}

/// Moved from `sherpa_kws.rs` — the KWS engine's discovery.
pub fn find_model(dir: &Path, stem: &str, int8: bool) -> Result<String, String> {
    let names = list_dir(dir)?;
    pick_model(&names, stem, int8)
        .map(|n| dir.join(n).display().to_string())
        .ok_or_else(|| {
            format!(
                "no {stem}*.onnx (int8={int8}) in {} — run scripts/fetch-models.sh",
                dir.display()
            )
        })
}

/// Classify a model directory. `model_type`: `auto` | `transducer` |
/// `zipformer2_ctc` | `nemo_ctc`. `int8`: prefer int8 exports (encoder + joiner;
/// the transducer decoder stays fp32 like sherpa's own examples); when no int8
/// export exists the fp32 one is used, and vice versa when `int8 = false` and
/// only int8 exists → error (an fp32 request must not silently run int8).
pub fn discover_online(
    dir: &Path,
    model_type: &str,
    int8: bool,
) -> Result<DiscoveredOnline, String> {
    if !dir.is_dir() {
        return Err(format!("model_dir {} is not a directory", dir.display()));
    }
    let names = list_dir(dir)?;
    let has = |stem: &str| {
        names
            .iter()
            .any(|n| n.starts_with(stem) && n.ends_with(".onnx"))
    };
    let kind = match model_type {
        "auto" => {
            if has("encoder") && has("decoder") && has("joiner") {
                "transducer"
            } else if has("ctc-") {
                "zipformer2_ctc"
            } else if has("model") {
                "nemo_ctc"
            } else {
                return Err(format!(
                    "{}: cannot classify — need encoder/decoder/joiner*.onnx, ctc-*.onnx or model*.onnx",
                    dir.display()
                ));
            }
        }
        "transducer" | "zipformer2_ctc" | "nemo_ctc" => model_type,
        other => {
            return Err(format!(
                "unknown model_type {other:?} (auto|transducer|zipformer2_ctc|nemo_ctc)"
            ))
        }
    };
    let pick = |stem: &str, want_int8: bool| -> Result<PathBuf, String> {
        if let Some(n) = pick_model(&names, stem, want_int8) {
            return Ok(dir.join(n));
        }
        if want_int8 {
            // fall back to fp32 when no int8 export ships
            if let Some(n) = pick_model(&names, stem, false) {
                return Ok(dir.join(n));
            }
        }
        Err(format!(
            "no {stem}*.onnx (int8={want_int8}) in {} — run scripts/fetch-models.sh",
            dir.display()
        ))
    };
    let files = match kind {
        "transducer" => OnlineModelFiles::Transducer {
            encoder: pick("encoder", int8)?,
            decoder: pick("decoder", false)?,
            joiner: pick("joiner", int8)?,
        },
        "zipformer2_ctc" => OnlineModelFiles::Zipformer2Ctc {
            model: pick("ctc-", int8)?,
        },
        _ => OnlineModelFiles::NemoCtc {
            model: pick("model", int8)?,
        },
    };
    let tokens = dir.join("tokens.txt");
    if !tokens.is_file() {
        return Err(format!("{}: tokens.txt missing", dir.display()));
    }
    let tokens_text =
        std::fs::read_to_string(&tokens).map_err(|e| format!("read tokens.txt: {e}"))?;
    let uppercase_vocab = tokens_text.chars().any(|c| c.is_ascii_uppercase());
    let opt = |name: &str| {
        let p = dir.join(name);
        if p.is_file() {
            Some(p)
        } else {
            None
        }
    };
    Ok(DiscoveredOnline {
        files,
        tokens,
        bpe_model: opt("bpe.model"),
        bpe_vocab: opt("bpe.vocab"),
        uppercase_vocab,
    })
}

/// sherpa's hotword machinery for BPE models needs `bpe.vocab`
/// (`piece<TAB>score` per line — the format `TokenTable::from_bpe_model`
/// already parses). Synthesize it from `bpe.model` when a directory ships only
/// the protobuf. Returns the vocab path.
pub fn ensure_bpe_vocab(dir: &Path, table: &TokenTable) -> Result<PathBuf, String> {
    let p = dir.join("bpe.vocab");
    if p.is_file() {
        return Ok(p);
    }
    let mut lines: Vec<String> = table
        .pieces()
        .map(|(piece, score)| format!("{piece}\t{score}"))
        .collect();
    lines.sort();
    std::fs::write(&p, lines.join("\n") + "\n")
        .map_err(|e| format!("write {} (needed for hotwords): {e}", p.display()))?;
    Ok(p)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn pick_prefers_exact_int8_flag_and_is_deterministic() {
        let n = names(&[
            "encoder-epoch-99-avg-1.onnx",
            "encoder-epoch-99-avg-1.int8.onnx",
            "decoder-epoch-99-avg-1.onnx",
            "joiner-epoch-99-avg-1.int8.onnx",
            "tokens.txt",
        ]);
        assert_eq!(
            pick_model(&n, "encoder", true).unwrap(),
            "encoder-epoch-99-avg-1.int8.onnx"
        );
        assert_eq!(
            pick_model(&n, "encoder", false).unwrap(),
            "encoder-epoch-99-avg-1.onnx"
        );
        assert!(pick_model(&n, "joiner", false).is_none());
        assert!(pick_model(&n, "tokens", true).is_none());
    }

    #[test]
    fn discover_classifies_real_layouts() {
        let td = tempfile::tempdir().unwrap();
        let d = td.path();
        // zipformer2 CTC (the production dir layout)
        std::fs::write(
            d.join("ctc-epoch-30-avg-3-chunk-16-left-128.int8.onnx"),
            b"x",
        )
        .unwrap();
        std::fs::write(d.join("tokens.txt"), "<blk> 0\n▁THE 1\n".as_bytes()).unwrap();
        let got = discover_online(d, "auto", true).unwrap();
        assert_eq!(got.files.kind(), "zipformer2_ctc");
        assert!(got.uppercase_vocab);
        assert!(got.bpe_model.is_none());
        // fp32 requested but only int8 present → refuse
        assert!(discover_online(d, "auto", false)
            .unwrap_err()
            .contains("int8=false"));

        // transducer, int8 preferred but only fp32 encoder ships → falls back
        let td2 = tempfile::tempdir().unwrap();
        let d = td2.path();
        for f in [
            "encoder-epoch-99-avg-1.onnx",
            "decoder-epoch-99-avg-1.onnx",
            "joiner-epoch-99-avg-1.onnx",
            "bpe.model",
        ] {
            std::fs::write(d.join(f), b"x").unwrap();
        }
        std::fs::write(d.join("tokens.txt"), "<blk> 0\n▁the 1\n".as_bytes()).unwrap();
        let got = discover_online(d, "auto", true).unwrap();
        assert!(got.files.is_transducer());
        assert!(!got.uppercase_vocab);
        assert!(got.bpe_model.is_some() && got.bpe_vocab.is_none());

        // nemo ctc
        let td3 = tempfile::tempdir().unwrap();
        let d = td3.path();
        std::fs::write(d.join("model.int8.onnx"), b"x").unwrap();
        std::fs::write(d.join("tokens.txt"), b"<blk> 0\n").unwrap();
        assert_eq!(
            discover_online(d, "auto", true).unwrap().files.kind(),
            "nemo_ctc"
        );
        // missing tokens.txt names the dir
        let td4 = tempfile::tempdir().unwrap();
        std::fs::write(td4.path().join("model.onnx"), b"x").unwrap();
        assert!(discover_online(td4.path(), "auto", true)
            .unwrap_err()
            .contains("tokens.txt"));
        assert!(discover_online(td4.path(), "bogus", true)
            .unwrap_err()
            .contains("model_type"));
    }
}
