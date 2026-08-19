//! Word-level keyword matching over an ASR token stream — the matcher shared by
//! the `online_asr` engine (streaming partials) and the fill-path verifier
//! (offline re-decode of a tail window).
//!
//! WHY WORDS, NOT COMPILED BPE PIECES. Every candidate model has its own
//! vocabulary (LibriSpeech BPE-500 uppercase, GigaSpeech BPE-500, NeMo SPE-1024
//! lowercase) and NeMo exports ship no `bpe.model`, so `kws_compile` cannot
//! produce piece targets for them; a transducer's beam search is also not
//! obliged to emit spm's canonical segmentation of a word, so piece-sequence
//! equality can miss what the text plainly says. Reconstructing words from the
//! `▁`-marked tokens keeps the token index → timestamp alignment that the
//! flattened `text` field loses.
//!
//! COMPLETION RULE. Only a COMPLETE word may match: a later token starting with
//! `▁` exists after it, or the caller says the stream is at an endpoint. The
//! trailing partial word never matches — `ai` inside "AIRLINE", `tar` inside
//! "TARGET" — which makes prefix-style false fires structurally impossible at
//! the cost of roughly one following token of latency. `Confirmed` only.

use smallvec::SmallVec;

use crate::config::KeywordConfig;
use crate::types::KeywordId;

/// Lowercase, keep `[a-z0-9]` and an inner apostrophe; everything else is a
/// separator. `"Tariffs,"` → `"tariffs"`, `"U.S."` → `"us"`? No — punctuation
/// inside a token splits it: `"U.S."` → `["u", "s"]`. Callers that need one
/// word per token get the FIRST piece; `words_from_tokens` glues sub-word
/// tokens before normalizing, so a `▁U` `.` `S` `.` sequence becomes `us`.
pub fn normalize_word(raw: &str) -> String {
    let lower = raw.to_lowercase();
    let mut out = String::with_capacity(lower.len());
    let chars: Vec<char> = lower.chars().collect();
    for (i, &c) in chars.iter().enumerate() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
        } else if c == '\''
            && i > 0
            && i + 1 < chars.len()
            && chars[i - 1].is_ascii_alphanumeric()
            && chars[i + 1].is_ascii_alphanumeric()
        {
            out.push(c);
        }
        // every other char (punctuation, `▁`, spaces) is dropped
    }
    out
}

/// Normalize a phrase into its word list (`"Apple Pay"` → `["apple","pay"]`).
pub fn normalize_phrase(text: &str) -> Vec<String> {
    text.split(|c: char| c.is_whitespace() || c == '-' || c == '/')
        .map(normalize_word)
        .filter(|w| !w.is_empty())
        .collect()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Word {
    pub text: String,
    /// Index of the first / last token composing this word.
    pub first_tok: usize,
    pub last_tok: usize,
    /// A later word-initial token exists, or the stream is at an endpoint.
    pub complete: bool,
}

/// Rebuild words from sub-word tokens. A token starting with `▁` (or a space)
/// begins a new word; anything else continues the current one. When no token
/// carries a boundary marker at all (character/CTC vocabularies), every token
/// is treated as its own word.
pub fn words_from_tokens(tokens: &[String], at_endpoint: bool) -> Vec<Word> {
    let any_marker = tokens
        .iter()
        .any(|t| t.starts_with('▁') || t.starts_with(' '));
    let mut words: Vec<Word> = Vec::new();
    for (i, tok) in tokens.iter().enumerate() {
        let starts =
            !any_marker || tok.starts_with('▁') || tok.starts_with(' ') || words.is_empty();
        if starts {
            if let Some(prev) = words.last_mut() {
                prev.complete = true;
            }
            words.push(Word {
                text: String::new(),
                first_tok: i,
                last_tok: i,
                complete: false,
            });
        }
        let w = words.last_mut().expect("pushed above");
        w.text.push_str(tok);
        w.last_tok = i;
    }
    for w in words.iter_mut() {
        w.text = normalize_word(&w.text);
    }
    if at_endpoint {
        if let Some(last) = words.last_mut() {
            last.complete = true;
        }
    }
    // drop words that normalized to nothing (pure punctuation tokens) but keep
    // their boundary effect on the previous word (already applied above)
    words.retain(|w| !w.text.is_empty());
    words
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Match {
    pub keyword: KeywordId,
    pub variant: usize,
    pub first_word: usize,
    pub last_word: usize,
}

/// Per-keyword normalized variant word n-grams.
#[derive(Debug, Clone)]
pub struct VariantMatcher {
    variants: Vec<Vec<Vec<String>>>, // keyword -> variant -> words
}

impl VariantMatcher {
    pub fn from_keywords(keywords: &[KeywordConfig]) -> Self {
        let variants = keywords
            .iter()
            .map(|k| {
                let mut vs: Vec<Vec<String>> =
                    k.variants.iter().map(|v| normalize_phrase(v)).collect();
                if vs.is_empty() {
                    vs.push(normalize_phrase(&k.phrase));
                }
                vs.retain(|v| !v.is_empty());
                vs
            })
            .collect();
        VariantMatcher { variants }
    }

    pub fn keyword_count(&self) -> usize {
        self.variants.len()
    }

    /// Scan `words[from_word..]` for complete-word matches; a match ends at a
    /// complete word (all words of a multi-word variant must be complete).
    /// Returns matches ordered by `last_word`.
    pub fn scan(&self, words: &[Word], from_word: usize) -> SmallVec<[Match; 4]> {
        let mut out: SmallVec<[Match; 4]> = SmallVec::new();
        for (ki, vs) in self.variants.iter().enumerate() {
            for (vi, v) in vs.iter().enumerate() {
                let n = v.len();
                if n == 0 || words.len() < n {
                    continue;
                }
                // a match may START before from_word as long as it ENDS at or after it
                let start_min = from_word.saturating_sub(n - 1);
                for s in start_min..=(words.len() - n) {
                    let e = s + n - 1;
                    if e < from_word {
                        continue;
                    }
                    if !words[e].complete {
                        continue;
                    }
                    if (0..n).all(|j| words[s + j].text == v[j] && words[s + j].complete) {
                        out.push(Match {
                            keyword: KeywordId(ki as u16),
                            variant: vi,
                            first_word: s,
                            last_word: e,
                        });
                    }
                }
            }
        }
        out.sort_by_key(|m| (m.last_word, m.keyword.0));
        out
    }

    /// Verifier path: does the keyword (any variant) occur in `text`?
    pub fn contains(&self, keyword: KeywordId, text: &str) -> Option<usize> {
        let words = normalize_phrase(text);
        let vs = self.variants.get(keyword.0 as usize)?;
        for (vi, v) in vs.iter().enumerate() {
            let n = v.len();
            if n == 0 || words.len() < n {
                continue;
            }
            if (0..=words.len() - n).any(|s| (0..n).all(|j| words[s + j] == v[j])) {
                return Some(vi);
            }
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Policy;

    fn kw(phrase: &str, variants: &[&str]) -> KeywordConfig {
        KeywordConfig {
            market_ticker: format!("T-{}", phrase.to_uppercase()),
            phrase: phrase.into(),
            variants: variants.iter().map(|s| s.to_string()).collect(),
            max_price_cents: 1,
            budget_contracts: 1,
            policy: Policy::ConfirmedOnly,
            rule_risk_haircut_cents: 0,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        }
    }

    fn toks(s: &[&str]) -> Vec<String> {
        s.iter().map(|t| t.to_string()).collect()
    }

    #[test]
    fn normalization_strips_punctuation_keeps_inner_apostrophe() {
        assert_eq!(normalize_word("Tariffs,"), "tariffs");
        assert_eq!(normalize_word("DON'T"), "don't");
        assert_eq!(normalize_word("'quoted'"), "quoted");
        assert_eq!(normalize_word("▁HELLO"), "hello");
        assert_eq!(
            normalize_phrase("Apple-Pay / open A.I."),
            vec!["apple", "pay", "open", "ai"]
        );
    }

    #[test]
    fn words_rebuild_from_marked_tokens_upper_and_lower() {
        let w = words_from_tokens(
            &toks(&["▁TAR", "I", "FF", "▁AND", "▁GU", "ID", "ANCE"]),
            false,
        );
        assert_eq!(
            w.iter().map(|w| w.text.as_str()).collect::<Vec<_>>(),
            vec!["tariff", "and", "guidance"]
        );
        assert!(w[0].complete && w[1].complete && !w[2].complete);
        assert_eq!((w[2].first_tok, w[2].last_tok), (4, 6));
        let w = words_from_tokens(&toks(&["▁tar", "iff", "s"]), true);
        assert_eq!(w[0].text, "tariffs");
        assert!(w[0].complete, "endpoint completes the trailing word");
    }

    #[test]
    fn unmarked_vocab_treats_each_token_as_a_word() {
        let w = words_from_tokens(&toks(&["hello", "world"]), false);
        assert_eq!(w.len(), 2);
        assert!(w[0].complete && !w[1].complete);
    }

    #[test]
    fn trailing_partial_word_never_matches() {
        let m = VariantMatcher::from_keywords(&[
            kw("a i", &["a i", "ai"]),
            kw("tariff", &["tariff", "tariffs"]),
        ]);
        // "AI" is the trailing partial of "AIRLINE" — must not match
        let w = words_from_tokens(&toks(&["▁THE", "▁AI"]), false);
        assert!(m.scan(&w, 0).is_empty());
        let w = words_from_tokens(&toks(&["▁THE", "▁AI", "R", "LINE", "▁IS"]), false);
        assert!(m.scan(&w, 0).is_empty(), "airline is not ai");
        // completed by a following word
        let w = words_from_tokens(&toks(&["▁THE", "▁AI", "▁IS"]), false);
        let hits = m.scan(&w, 0);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].keyword, KeywordId(0));
        assert_eq!(hits[0].variant, 1);
        // completed by endpoint
        let w = words_from_tokens(&toks(&["▁NEW", "▁TAR", "IFF", "S"]), true);
        let hits = m.scan(&w, 0);
        assert_eq!(hits.len(), 1);
        assert_eq!((hits[0].keyword, hits[0].variant), (KeywordId(1), 1));
    }

    #[test]
    fn multi_word_variant_and_watermark() {
        let m = VariantMatcher::from_keywords(&[kw("apple pay", &["apple pay"])]);
        let w = words_from_tokens(&toks(&["▁WITH", "▁APPLE", "▁PAY", "▁TODAY"]), false);
        let hits = m.scan(&w, 0);
        assert_eq!(hits.len(), 1);
        assert_eq!((hits[0].first_word, hits[0].last_word), (1, 2));
        // watermark past the match end → no re-emit
        assert!(m.scan(&w, 3).is_empty());
        // watermark at the last word of the match still catches it (match ends >= from_word)
        assert_eq!(m.scan(&w, 2).len(), 1);
        // "apple" alone or "pay" alone never match
        let w = words_from_tokens(&toks(&["▁APPLE", "▁TODAY", "▁PAY", "▁NOW"]), false);
        assert!(m.scan(&w, 0).is_empty());
    }

    #[test]
    fn contains_is_the_verifier_path() {
        let m = VariantMatcher::from_keywords(&[kw(
            "headwind",
            &["headwind", "headwinds", "head wind"],
        )]);
        assert_eq!(
            m.contains(KeywordId(0), "some head wind, honestly."),
            Some(2)
        );
        assert_eq!(m.contains(KeywordId(0), "HEADWINDS ahead"), Some(1));
        assert_eq!(m.contains(KeywordId(0), "tailwinds only"), None);
        assert_eq!(m.contains(KeywordId(3), "headwind"), None);
    }
}
