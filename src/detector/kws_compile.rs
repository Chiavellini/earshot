//! Keyword → sherpa-onnx token-sequence compilation (spec §5.4, M1).
//!
//! The KWS transducer emits SentencePiece tokens, so keyword entries must
//! match the model's own segmentation exactly — greedy longest-match against
//! tokens.txt diverges on 4 of 12 known vectors (see VERIFICATIONS.md M1
//! pins). The bundled `bpe.model` is (despite its name) a spm UNIGRAM model,
//! so this module implements unigram Viterbi segmentation: the split of each
//! word maximizing the summed piece log-probs, exactly what spm computes.
//!
//! Fast (partial-word) entries are NOT an independent encoding of the prefix
//! text: "artificial intel" encodes to `… ▁IN T EL` while the transducer
//! emits `▁IN TE LL IG ENCE` mid-word — an independently-encoded prefix would
//! never match. Fast entries are the full variant's token sequence CUT at the
//! first token boundary that covers the configured prefix text.

use std::collections::HashMap;
use std::path::Path;

use thiserror::Error;

use crate::config::KeywordConfig;
use crate::types::{KeywordId, Tier};

#[derive(Debug, Error)]
pub enum CompileError {
    #[error("failed to read {path}: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("bpe.model parse error: {0}")]
    BpeModel(String),
    #[error("keyword {phrase:?}: cannot BPE-encode {text:?}: symbol {symbol:?} not in vocab")]
    Unencodable {
        phrase: String,
        text: String,
        symbol: String,
    },
    #[error("keyword {phrase:?}: fast_prefix {prefix:?} covers the whole variant {variant:?} — it would duplicate the Confirmed entry, shorten it")]
    PrefixNotShorter {
        phrase: String,
        prefix: String,
        variant: String,
    },
    #[error("keyword {phrase:?}: fast_prefix {prefix:?} is not a prefix of any variant")]
    PrefixMatchesNothing { phrase: String, prefix: String },
}

/// Longest piece in the gigaspeech-500 vocab is well under this; bounds the
/// Viterbi inner loop.
const MAX_PIECE_CHARS: usize = 24;

/// SentencePiece pieces + unigram log-prob scores, parsed straight out of the
/// model's `bpe.model` protobuf (no sentencepiece dependency).
pub struct TokenTable {
    scores: HashMap<String, f32>,
}

impl TokenTable {
    pub fn from_bpe_model(path: &Path) -> Result<TokenTable, CompileError> {
        let bytes = std::fs::read(path).map_err(|source| CompileError::Io {
            path: path.display().to_string(),
            source,
        })?;
        let mut scores = HashMap::new();
        let mut buf = &bytes[..];
        while !buf.is_empty() {
            let (key, rest) = read_varint(buf)?;
            buf = rest;
            let field = key >> 3;
            let wire = key & 7;
            if field == 1 && wire == 2 {
                // repeated SentencePiece { piece = 1 (string), score = 2 (float) }
                let (len, rest) = read_varint(buf)?;
                buf = rest;
                let (msg, rest) = split_checked(buf, len as usize)?;
                buf = rest;
                if let Some((piece, score)) = parse_sentence_piece(msg)? {
                    scores.insert(piece, score);
                }
            } else {
                buf = skip_field(buf, wire)?;
            }
        }
        if scores.is_empty() {
            return Err(CompileError::BpeModel("no pieces found".into()));
        }
        Ok(TokenTable { scores })
    }

    /// SentencePiece unigram encode: per word (with the `▁` marker glued to
    /// its front), Viterbi over piece boundaries maximizing summed piece
    /// log-probs. Matches spm's own segmentation (pinned vectors below).
    pub fn encode(&self, phrase: &str, text: &str) -> Result<Vec<String>, CompileError> {
        let mut out = Vec::new();
        for word in text.trim().to_uppercase().split_whitespace() {
            let chars: Vec<char> = std::iter::once('▁').chain(word.chars()).collect();
            let n = chars.len();
            // dp[i] = best (score, piece-start) covering chars[..i].
            let mut dp: Vec<Option<(f32, usize)>> = vec![None; n + 1];
            dp[0] = Some((0.0, 0));
            for end in 1..=n {
                for start in end.saturating_sub(MAX_PIECE_CHARS)..end {
                    let Some((prev, _)) = dp[start] else { continue };
                    let piece: String = chars[start..end].iter().collect();
                    if let Some(&score) = self.scores.get(&piece) {
                        let total = prev + score;
                        if dp[end].map_or(true, |(s, _)| total > s) {
                            dp[end] = Some((total, start));
                        }
                    }
                }
            }
            if dp[n].is_none() {
                return Err(CompileError::Unencodable {
                    phrase: phrase.into(),
                    text: text.into(),
                    symbol: word.into(),
                });
            }
            let mut pieces_rev = Vec::new();
            let mut end = n;
            while end > 0 {
                let (_, start) = dp[end].unwrap();
                pieces_rev.push(chars[start..end].iter().collect::<String>());
                end = start;
            }
            out.extend(pieces_rev.into_iter().rev());
        }
        Ok(out)
    }
}

fn read_varint(buf: &[u8]) -> Result<(u64, &[u8]), CompileError> {
    let mut v = 0u64;
    for (i, &b) in buf.iter().enumerate().take(10) {
        v |= u64::from(b & 0x7f) << (7 * i);
        if b & 0x80 == 0 {
            return Ok((v, &buf[i + 1..]));
        }
    }
    Err(CompileError::BpeModel("truncated varint".into()))
}

fn split_checked(buf: &[u8], len: usize) -> Result<(&[u8], &[u8]), CompileError> {
    if len > buf.len() {
        return Err(CompileError::BpeModel("truncated message".into()));
    }
    Ok(buf.split_at(len))
}

fn skip_field(buf: &[u8], wire: u64) -> Result<&[u8], CompileError> {
    match wire {
        0 => read_varint(buf).map(|(_, rest)| rest),
        1 => split_checked(buf, 8).map(|(_, rest)| rest),
        2 => {
            let (len, rest) = read_varint(buf)?;
            split_checked(rest, len as usize).map(|(_, rest)| rest)
        }
        5 => split_checked(buf, 4).map(|(_, rest)| rest),
        w => Err(CompileError::BpeModel(format!("unsupported wire type {w}"))),
    }
}

fn parse_sentence_piece(mut msg: &[u8]) -> Result<Option<(String, f32)>, CompileError> {
    let mut piece = None;
    let mut score = 0f32;
    while !msg.is_empty() {
        let (key, rest) = read_varint(msg)?;
        msg = rest;
        match (key >> 3, key & 7) {
            (1, 2) => {
                let (len, rest) = read_varint(msg)?;
                let (s, rest) = split_checked(rest, len as usize)?;
                msg = rest;
                piece = Some(String::from_utf8_lossy(s).into_owned());
            }
            (2, 5) => {
                let (s, rest) = split_checked(msg, 4)?;
                msg = rest;
                score = f32::from_le_bytes(s.try_into().unwrap());
            }
            (_, wire) => msg = skip_field(msg, wire)?,
        }
    }
    Ok(piece.map(|p| (p, score)))
}

#[derive(Debug, Clone, PartialEq)]
pub struct CompiledEntry {
    pub keyword: KeywordId,
    pub tier: Tier,
    pub tokens: Vec<String>,
    pub threshold: f32,
    pub boost: f32,
    /// Round-trips through sherpa's `@label` — `parse_label` inverts it.
    pub label: String,
}

/// Compile the config keyword table into sherpa keyword entries. Confirmed =
/// full encode per variant; Fast = the first prefix-matching variant's tokens
/// cut at the first boundary covering the prefix text.
pub fn compile(
    keywords: &[KeywordConfig],
    table: &TokenTable,
    default_threshold: f32,
    default_boost: f32,
) -> Result<Vec<CompiledEntry>, CompileError> {
    let mut entries = Vec::new();
    for (i, kw) in keywords.iter().enumerate() {
        let id = KeywordId(i as u16);
        let boost = kw.boost.unwrap_or(default_boost);
        for (v, variant) in kw.variants.iter().enumerate() {
            entries.push(CompiledEntry {
                keyword: id,
                tier: Tier::Confirmed,
                tokens: table.encode(&kw.phrase, variant)?,
                threshold: kw.confirmed_threshold.unwrap_or(default_threshold),
                boost,
                label: format!("kw{i}_conf{v}"),
            });
        }
        for (p, prefix) in kw.fast_prefixes.iter().enumerate() {
            let variant = kw
                .variants
                .iter()
                .find(|v| v.to_lowercase().starts_with(&prefix.to_lowercase()))
                .ok_or_else(|| CompileError::PrefixMatchesNothing {
                    phrase: kw.phrase.clone(),
                    prefix: prefix.clone(),
                })?;
            let full = table.encode(&kw.phrase, variant)?;
            let cut = prefix_cut(&full, prefix);
            if cut >= full.len() {
                return Err(CompileError::PrefixNotShorter {
                    phrase: kw.phrase.clone(),
                    prefix: prefix.clone(),
                    variant: variant.clone(),
                });
            }
            entries.push(CompiledEntry {
                keyword: id,
                tier: Tier::Fast,
                tokens: full[..cut].to_vec(),
                threshold: kw.fast_threshold.unwrap_or(default_threshold),
                boost,
                label: format!("kw{i}_fast{p}"),
            });
        }
    }
    Ok(entries)
}

/// First token boundary whose accumulated text covers the prefix. Token text
/// counts `▁` as the inter-word space; the leading `▁` of the first word is
/// not part of the prefix text. Returns `tokens.len() + 1` when the prefix is
/// not covered by any boundary shorter than the full word (the `compile`
/// callers treat `cut >= tokens.len()` as `PrefixNotShorter`). Public so the
/// `compile-keywords` diagnostic can report per-candidate cuts without running
/// the whole `compile` (which aborts on the first bad prefix).
pub fn prefix_cut(tokens: &[String], prefix: &str) -> usize {
    // Count prefix chars the way the token stream renders them: any run of
    // whitespace between words is exactly one `▁` (encode() splits on
    // split_whitespace), so a doubled space / tab in config must count as ONE
    // char — counting it raw pushed the cut one token too far.
    let upper = prefix.to_uppercase();
    let words: Vec<&str> = upper.split_whitespace().collect();
    let target: usize = words.iter().map(|w| w.chars().count()).sum::<usize>()
        + words.len().saturating_sub(1);
    let mut covered = 0usize; // prefix chars covered (spaces count once)
    for (i, tok) in tokens.iter().enumerate() {
        for c in tok.chars() {
            covered += 1;
            if i == 0 && c == '▁' {
                covered -= 1; // leading word marker isn't prefix text
            }
        }
        if covered >= target {
            return i + 1;
        }
    }
    tokens.len() + 1
}

/// One sherpa keywords line per entry: `TOK TOK :boost #threshold @label`
/// (format pinned empirically, VERIFICATIONS.md M1 pins).
pub fn render_keywords(entries: &[CompiledEntry]) -> String {
    let mut s = String::new();
    for e in entries {
        s.push_str(&e.tokens.join(" "));
        s.push_str(&format!(" :{} #{} @{}\n", e.boost, e.threshold, e.label));
    }
    s
}

/// Invert the `@label` scheme. Unknown labels return `None` (caller logs).
pub fn parse_label(label: &str) -> Option<(KeywordId, Tier)> {
    let rest = label.strip_prefix("kw")?;
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    let idx: u16 = digits.parse().ok()?;
    let rest = &rest[digits.len()..];
    if rest.starts_with("_fast") {
        Some((KeywordId(idx), Tier::Fast))
    } else if rest.starts_with("_conf") {
        Some((KeywordId(idx), Tier::Confirmed))
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MODEL_DIR: &str = "models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01";

    fn table() -> Option<TokenTable> {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join(MODEL_DIR)
            .join("bpe.model");
        if !path.exists() {
            if std::env::var_os("EARSHOT_REQUIRE_MODELS").is_some() {
                panic!("EARSHOT_REQUIRE_MODELS set but {} missing — run scripts/fetch-models.sh", path.display());
            }
            eprintln!("SKIP: {} not present — run scripts/fetch-models.sh", path.display());
            return None;
        }
        Some(TokenTable::from_bpe_model(&path).unwrap())
    }

    fn enc(t: &TokenTable, s: &str) -> String {
        t.encode("test", s).unwrap().join(" ")
    }

    /// Vectors pinned against python sentencepiece AND sherpa's own
    /// keywords_raw.txt → keywords.txt pairs (VERIFICATIONS.md M1 pins).
    /// Greedy longest-match fails 4 of these — that's why BPE is required.
    #[test]
    fn bpe_matches_sentencepiece_pinned_vectors() {
        let Some(t) = table() else { return };
        assert_eq!(enc(&t, "HELLO WORLD"), "▁HE LL O ▁WORLD");
        assert_eq!(enc(&t, "HI GOOGLE"), "▁HI ▁GO O G LE");
        assert_eq!(enc(&t, "HEY SIRI"), "▁HE Y ▁S I RI");
        assert_eq!(enc(&t, "ALEXA"), "▁A LE X A");
        assert_eq!(enc(&t, "LOVE AND PEACE"), "▁LOVE ▁AND ▁P E A CE");
        assert_eq!(enc(&t, "PLAY MUSIC"), "▁PLAY ▁MU S IC");
        assert_eq!(enc(&t, "GO HOME"), "▁GO ▁HOME");
        assert_eq!(enc(&t, "HAPPY NEW YEAR"), "▁HA PP Y ▁NEW ▁YEAR");
        assert_eq!(enc(&t, "MERRY CHRISTMAS"), "▁ME R RY ▁ CH R IST MA S");
        assert_eq!(enc(&t, "LIGHT UP"), "▁ L IGHT ▁UP");
        assert_eq!(enc(&t, "LOVELY CHILD"), "▁LOVE LY ▁CHI L D");
        assert_eq!(enc(&t, "recession"), "▁RE CE S S ION");
        assert_eq!(enc(&t, "artificial intelligence"), "▁A R T IF IC I AL ▁IN TE LL IG ENCE");
    }

    fn kw(phrase: &str, variants: &[&str], prefixes: &[&str]) -> KeywordConfig {
        KeywordConfig {
            market_ticker: "KX-T".into(),
            phrase: phrase.into(),
            variants: variants.iter().map(|s| s.to_string()).collect(),
            max_price_cents: 97,
            budget_contracts: 200,
            policy: crate::config::Policy::TwoTier,
            rule_risk_haircut_cents: 0,
            fast_prefixes: prefixes.iter().map(|s| s.to_string()).collect(),
            fast_threshold: Some(0.15),
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        }
    }

    #[test]
    fn fast_entry_is_token_prefix_of_confirmed_never_reencoded() {
        let Some(t) = table() else { return };
        let entries =
            compile(&[kw("recession", &["recession", "recessions"], &["reces"])], &t, 0.25, 2.0)
                .unwrap();
        assert_eq!(entries.len(), 3);
        let conf: Vec<_> = entries.iter().filter(|e| e.tier == Tier::Confirmed).collect();
        let fast: Vec<_> = entries.iter().filter(|e| e.tier == Tier::Fast).collect();
        assert_eq!(conf[0].tokens.join(" "), "▁RE CE S S ION");
        // "reces" covers ▁RE CE S exactly — a strict token-prefix of the full word.
        assert_eq!(fast[0].tokens.join(" "), "▁RE CE S");
        assert_eq!(fast[0].threshold, 0.15);
        assert_eq!(conf[0].threshold, 0.25);

        // The trap case: "artificial intel" must be cut from the FULL word's
        // tokens (… ▁IN TE LL), never independently encoded (… ▁IN T EL).
        let entries = compile(
            &[kw("ai", &["artificial intelligence"], &["artificial intel"])],
            &t,
            0.25,
            2.0,
        )
        .unwrap();
        let fast: Vec<_> = entries.iter().filter(|e| e.tier == Tier::Fast).collect();
        assert_eq!(fast[0].tokens.join(" "), "▁A R T IF IC I AL ▁IN TE LL");
    }

    #[test]
    fn prefix_covering_whole_variant_is_rejected() {
        let Some(t) = table() else { return };
        let err = compile(&[kw("go", &["go"], &["go"])], &t, 0.25, 2.0).unwrap_err();
        assert!(matches!(err, CompileError::PrefixNotShorter { .. }), "{err}");
    }

    /// Pure `prefix_cut` edge cases — no model needed. Whitespace runs in the
    /// prefix must count as one inter-word `▁`, exactly as encode() renders
    /// them, or the Fast cut lands one token too far.
    #[test]
    fn prefix_cut_collapses_whitespace_runs() {
        let toks = |s: &str| s.split(' ').map(String::from).collect::<Vec<_>>();
        // "artificial intel" over ▁A R T IF IC I AL ▁IN TE LL IG ENCE.
        let full = toks("▁A R T IF IC I AL ▁IN TE LL IG ENCE");
        let clean = prefix_cut(&full, "artificial intel");
        assert_eq!(clean, 10); // …▁IN TE LL covers "intel"
        // A doubled space or tab must give the SAME cut, not one token more.
        assert_eq!(prefix_cut(&full, "artificial  intel"), clean);
        assert_eq!(prefix_cut(&full, "artificial\tintel"), clean);
        assert_eq!(prefix_cut(&full, " artificial intel "), clean);
        // Single-word prefixes unchanged.
        let rec = toks("▁RE CE S S ION");
        assert_eq!(prefix_cut(&rec, "reces"), 3);
        assert_eq!(prefix_cut(&rec, "recession"), 5);
        // Uncoverable prefix still reports tokens.len() + 1.
        assert_eq!(prefix_cut(&rec, "recessionary"), rec.len() + 1);
    }

    #[test]
    fn render_and_label_round_trip() {
        let Some(t) = table() else { return };
        let entries = compile(&[kw("recession", &["recession"], &["reces"])], &t, 0.25, 2.0).unwrap();
        let rendered = render_keywords(&entries);
        assert!(rendered.contains("▁RE CE S S ION :2 #0.25 @kw0_conf0\n"), "{rendered}");
        assert!(rendered.contains("▁RE CE S :2 #0.15 @kw0_fast0\n"), "{rendered}");
        for e in &entries {
            assert_eq!(parse_label(&e.label), Some((e.keyword, e.tier)));
        }
        assert_eq!(parse_label("kw12_fast3"), Some((KeywordId(12), Tier::Fast)));
        assert_eq!(parse_label("garbage"), None);
        assert_eq!(parse_label("kw3_other"), None);
    }
}
