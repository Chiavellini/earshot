//! Streaming CTC keyword scorer (the custom low-lookahead spotter's decode
//! core, M-detector milestone). Runs on per-frame CTC log-posteriors emitted by
//! a streaming encoder (see `streaming_ctc.rs`), firing a keyword the instant
//! its token sequence completes — natively on frame posteriors, so it yields a
//! real acoustic confidence instead of the sherpa KWS engine's hard-coded 1.0.
//!
//! One `EntryState` per compiled keyword entry (a `(keyword, tier)` with a token
//! target). Fast entries carry the *cut prefix* token sequence and Confirmed the
//! full word (identical tier semantics to `kws_compile.rs`), so Fast fires
//! earlier from the same single posterior stream — no second decoder.
//!
//! ## Algorithm — start-anytime Viterbi over the CTC lattice
//!
//! For a target `t0..t{k-1}` we score the blank-expanded lattice
//! `[blank, t0, blank, t1, blank, ..., t{k-1}, blank]` (length `L = 2k+1`) with
//! a log-space Viterbi forward variable `alpha[0..L]`, updated once per frame.
//! A virtual filler state with log-prob 0 feeds states 0 and 1 every frame, so
//! the keyword may *start at any frame* (spotting, not full-utterance
//! alignment). The detection score is `max(alpha[L-2], alpha[L-1])` — the best
//! path that has consumed the final token (`L-2`) or its trailing blank
//! (`L-1`) — so it fires the frame the last token peaks, not at endpointing.
//!
//! Confidence is `exp(score / k)`: the geometric-mean per-token posterior along
//! the best path (blanks contribute ≈0), clamped to [0,1]. Gating is per-entry
//! `threshold`; refractory dedupe is per `(keyword, tier)`, matching the sherpa
//! engine's contract.

use smallvec::SmallVec;

use crate::types::{KeywordId, Tier};

/// A compiled keyword entry's firing target: a token-id sequence for one tier.
#[derive(Debug, Clone)]
pub struct KeywordTarget {
    pub keyword: KeywordId,
    pub tier: Tier,
    /// Model-vocab token ids (blank excluded). Adjacent repeats are fine — the
    /// CTC lattice forces a blank between identical tokens.
    pub tokens: Vec<u32>,
    /// Confidence threshold in [0,1] for this entry to fire.
    pub threshold: f32,
}

/// A keyword firing produced by [`CtcKeywordScorer::accept_frame`].
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Fired {
    pub keyword: KeywordId,
    pub tier: Tier,
    pub confidence: f32,
    /// Output-frame index at which the keyword completed.
    pub frame: u64,
}

/// Why an entry did not fire — the number the scorer used to throw away.
///
/// Before 2026-07-29 a sub-threshold entry was a bare `continue` (no log, no
/// counter), so "0 detections" was an unexplainable number. That cost us: the
/// recall gate found 12 of 52 market words scoring ZERO on clean studio TTS
/// (`docs/BAKEOFF.md` §RECALL GATE) with no way to tell WHICH of two opposite
/// failures each one was:
///
///   * `peak_confidence` just under `threshold`  ⇒ a NEAR-MISS. The model heard
///     the word; the operating point rejected it. Threshold/scoring territory.
///   * some `piece_best_logp[i]` deeply negative  ⇒ the model never emitted that
///     piece at all, so `exp(score/k)` — a geometric mean — is dragged to ~0 no
///     matter how strongly the other k−1 pieces peak. Spelling/variant
///     territory, and NOT fixable by any threshold.
///
/// Diagnosis only: recorded on every frame, never consulted for firing.
#[derive(Debug, Clone)]
pub struct EntryExplain {
    pub keyword: KeywordId,
    pub tier: Tier,
    /// Token pieces this entry targets (model vocab ids, blank excluded).
    pub tokens: Vec<u32>,
    /// Number of pieces — the geometric-mean divisor.
    pub k: usize,
    pub threshold: f32,
    /// Best confidence this entry ever reached, fired or not.
    pub peak_confidence: f32,
    /// Frame at which `peak_confidence` occurred.
    pub peak_frame: u64,
    /// Per-piece best log-posterior seen anywhere in the stream, aligned to
    /// `tokens`. The smallest entry here is what sinks the geometric mean.
    pub piece_best_logp: Vec<f32>,
    /// Frame of each `piece_best_logp`.
    pub piece_best_frame: Vec<u64>,
    pub fired: bool,
}

/// Per-entry streaming Viterbi state.
struct EntryState {
    keyword: KeywordId,
    tier: Tier,
    /// Blank-expanded lattice symbols (`sym[s]`), length `L = 2k+1`.
    sym: Vec<usize>,
    /// `alpha[s]`: best-path log-prob ending in lattice state `s` at the current
    /// frame. `f32::NEG_INFINITY` means unreachable.
    alpha: Vec<f32>,
    k: usize,
    threshold: f32,
    last_fire_frame: Option<u64>,
    // ── diagnosis-only fields (never read by the firing decision) ────────────
    tokens: Vec<u32>,
    peak_confidence: f32,
    peak_frame: u64,
    piece_best_logp: Vec<f32>,
    piece_best_frame: Vec<u64>,
    fired: bool,
}

impl EntryState {
    fn new(t: &KeywordTarget, blank: usize) -> EntryState {
        let k = t.tokens.len();
        assert!(k > 0, "keyword target must have at least one token");
        let mut sym = Vec::with_capacity(2 * k + 1);
        for i in 0..k {
            sym.push(blank);
            sym.push(t.tokens[i] as usize);
        }
        sym.push(blank);
        let l = sym.len();
        EntryState {
            keyword: t.keyword,
            tier: t.tier,
            sym,
            alpha: vec![f32::NEG_INFINITY; l],
            k,
            threshold: t.threshold,
            last_fire_frame: None,
            tokens: t.tokens.clone(),
            peak_confidence: 0.0,
            peak_frame: 0,
            piece_best_logp: vec![f32::NEG_INFINITY; k],
            piece_best_frame: vec![0; k],
            fired: false,
        }
    }

    fn reset(&mut self) {
        for a in &mut self.alpha {
            *a = f32::NEG_INFINITY;
        }
        self.last_fire_frame = None;
        self.peak_confidence = 0.0;
        self.peak_frame = 0;
        for p in &mut self.piece_best_logp {
            *p = f32::NEG_INFINITY;
        }
        for f in &mut self.piece_best_frame {
            *f = 0;
        }
        self.fired = false;
    }

    fn explain(&self) -> EntryExplain {
        EntryExplain {
            keyword: self.keyword,
            tier: self.tier,
            tokens: self.tokens.clone(),
            k: self.k,
            threshold: self.threshold,
            peak_confidence: self.peak_confidence,
            peak_frame: self.peak_frame,
            piece_best_logp: self.piece_best_logp.clone(),
            piece_best_frame: self.piece_best_frame.clone(),
            fired: self.fired,
        }
    }
}

/// Scores a fixed set of keyword targets against a streaming posterior feed.
pub struct CtcKeywordScorer {
    blank: usize,
    entries: Vec<EntryState>,
}

impl CtcKeywordScorer {
    /// `blank` is the CTC blank token id (0 for sherpa/k2 exports).
    pub fn new(targets: &[KeywordTarget], blank: usize) -> CtcKeywordScorer {
        let entries = targets.iter().map(|t| EntryState::new(t, blank)).collect();
        CtcKeywordScorer { blank, entries }
    }

    /// Clear all forward state (e.g. between bench runs). Not needed mid-stream —
    /// the refractory table is the dedupe, matching the detector contract.
    pub fn reset(&mut self) {
        for e in &mut self.entries {
            e.reset();
        }
    }

    /// Advance every entry by one frame of CTC log-posteriors and return any
    /// keywords that fire at this frame. `refractory_frames` suppresses a repeat
    /// fire of the same `(keyword, tier)` within that many frames.
    pub fn accept_frame(
        &mut self,
        frame_idx: u64,
        log_post: &[f32],
        refractory_frames: u64,
    ) -> SmallVec<[Fired; 2]> {
        let mut out = SmallVec::new();
        let blank = self.blank;
        for e in &mut self.entries {
            let l = e.sym.len();
            // Viterbi update into a scratch buffer (needs the *previous* alpha).
            let mut next = vec![f32::NEG_INFINITY; l];
            for s in 0..l {
                // Predecessors: self-loop, s-1, and (for a non-blank symbol that
                // differs from s-2) the skip s-2. Plus the start-anytime filler
                // (log-prob 0) into the first blank (s=0) and first token (s=1).
                let mut best = e.alpha[s];
                if s >= 1 {
                    best = best.max(e.alpha[s - 1]);
                }
                if s >= 2 && e.sym[s] != blank && e.sym[s] != e.sym[s - 2] {
                    best = best.max(e.alpha[s - 2]);
                }
                if s <= 1 {
                    best = best.max(0.0); // filler: keyword may start here
                }
                if best > f32::NEG_INFINITY {
                    next[s] = log_post[e.sym[s]] + best;
                }
            }
            e.alpha = next;

            // ── diagnosis-only bookkeeping ───────────────────────────────────
            // Per-piece best posterior, so a miss can name the piece the model
            // never emitted. This is the quantity that decides whether a dead
            // word is a near-miss or a spelling mismatch, and it is why the two
            // are distinguishable at all. Pure observation: nothing below reads
            // these fields, so firing behaviour is bit-identical.
            for (i, &tok) in e.tokens.iter().enumerate() {
                let lp = log_post[tok as usize];
                if lp > e.piece_best_logp[i] {
                    e.piece_best_logp[i] = lp;
                    e.piece_best_frame[i] = frame_idx;
                }
            }

            // Detection score: best path having consumed the last token or its
            // trailing blank.
            let score = e.alpha[l - 1].max(e.alpha[l - 2]);
            if score <= f32::NEG_INFINITY {
                continue;
            }
            let confidence = (score / e.k as f32).exp().clamp(0.0, 1.0);
            // Record the peak BEFORE the threshold gate — the whole point is to
            // keep the number the gate used to discard.
            if confidence > e.peak_confidence {
                e.peak_confidence = confidence;
                e.peak_frame = frame_idx;
            }
            if confidence < e.threshold {
                continue;
            }
            if let Some(last) = e.last_fire_frame {
                if frame_idx.saturating_sub(last) < refractory_frames {
                    continue;
                }
            }
            e.last_fire_frame = Some(frame_idx);
            e.fired = true;
            out.push(Fired { keyword: e.keyword, tier: e.tier, confidence, frame: frame_idx });
        }
        out
    }

    /// Per-entry diagnosis after a stream: what each target's best evidence was,
    /// including the sub-threshold entries the firing path drops silently.
    ///
    /// Read this to answer "why didn't it fire?" — see [`EntryExplain`].
    pub fn explain(&self) -> Vec<EntryExplain> {
        self.entries.iter().map(|e| e.explain()).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const V: usize = 6; // vocab: blank=0, tokens 1..=5

    /// A log-posterior frame putting mass `p` on `token` and spreading the rest
    /// uniformly over the other symbols. `token = 0` means a blank/silence frame.
    fn frame(token: usize, p: f32) -> Vec<f32> {
        let rest = (1.0 - p) / (V as f32 - 1.0);
        (0..V).map(|i| if i == token { p.ln() } else { rest.ln() }).collect()
    }

    fn target(kw: u16, tier: Tier, tokens: &[u32], threshold: f32) -> KeywordTarget {
        KeywordTarget { keyword: KeywordId(kw), tier, tokens: tokens.to_vec(), threshold }
    }

    /// Drive a frame sequence and collect all fires (refractory 100 frames).
    fn run(scorer: &mut CtcKeywordScorer, frames: &[Vec<f32>]) -> Vec<Fired> {
        let mut fires = Vec::new();
        for (i, f) in frames.iter().enumerate() {
            fires.extend(scorer.accept_frame(i as u64, f, 100));
        }
        fires
    }

    #[test]
    fn full_keyword_fires_when_last_token_completes() {
        let mut s = CtcKeywordScorer::new(&[target(0, Tier::Confirmed, &[1, 2, 3], 0.5)], 0);
        // silence, then tokens 1,2,3 spaced by blanks, then silence
        let frames = vec![
            frame(0, 0.95),
            frame(0, 0.95),
            frame(1, 0.9),
            frame(0, 0.9),
            frame(2, 0.9),
            frame(0, 0.9),
            frame(3, 0.9),
            frame(0, 0.95),
        ];
        let fires = run(&mut s, &frames);
        assert_eq!(fires.len(), 1, "exactly one fire; got {fires:?}");
        assert_eq!(fires[0].keyword, KeywordId(0));
        assert_eq!(fires[0].tier, Tier::Confirmed);
        // fires at the frame token 3 peaks (index 6), not at endpointing
        assert_eq!(fires[0].frame, 6);
        assert!(fires[0].confidence >= 0.5);
    }

    #[test]
    fn fast_prefix_fires_but_confirmed_does_not_without_last_token() {
        // Same keyword, two tiers: Fast = cut prefix [1,2], Confirmed = [1,2,3].
        let mut s = CtcKeywordScorer::new(
            &[
                target(0, Tier::Fast, &[1, 2], 0.5),
                target(0, Tier::Confirmed, &[1, 2, 3], 0.5),
            ],
            0,
        );
        // only 1,2 spoken — token 3 never arrives
        let frames = vec![
            frame(0, 0.95),
            frame(1, 0.9),
            frame(0, 0.9),
            frame(2, 0.9),
            frame(0, 0.95),
            frame(0, 0.95),
        ];
        let fires = run(&mut s, &frames);
        assert!(fires.iter().any(|f| f.tier == Tier::Fast), "Fast should fire: {fires:?}");
        assert!(
            !fires.iter().any(|f| f.tier == Tier::Confirmed),
            "Confirmed must NOT fire without token 3: {fires:?}"
        );
    }

    #[test]
    fn silence_never_fires() {
        let mut s = CtcKeywordScorer::new(&[target(0, Tier::Confirmed, &[1, 2, 3], 0.5)], 0);
        let frames: Vec<_> = (0..20).map(|_| frame(0, 0.98)).collect();
        assert!(run(&mut s, &frames).is_empty());
    }

    #[test]
    fn wrong_tokens_never_fire() {
        let mut s = CtcKeywordScorer::new(&[target(0, Tier::Confirmed, &[1, 2, 3], 0.5)], 0);
        let frames = vec![frame(4, 0.9), frame(0, 0.9), frame(5, 0.9), frame(0, 0.9), frame(4, 0.9)];
        assert!(run(&mut s, &frames).is_empty());
    }

    #[test]
    fn low_confidence_below_threshold_does_not_fire() {
        // tokens present but with weak mass -> confidence below a high threshold
        let mut s = CtcKeywordScorer::new(&[target(0, Tier::Confirmed, &[1, 2, 3], 0.85)], 0);
        let frames = vec![frame(1, 0.35), frame(2, 0.35), frame(3, 0.35)];
        assert!(run(&mut s, &frames).is_empty(), "weak match must stay below 0.85");

        // strong mass clears the same threshold
        let mut s2 = CtcKeywordScorer::new(&[target(0, Tier::Confirmed, &[1, 2, 3], 0.85)], 0);
        let strong = vec![frame(1, 0.97), frame(2, 0.97), frame(3, 0.97)];
        assert_eq!(run(&mut s2, &strong).len(), 1);
    }

    #[test]
    fn refractory_suppresses_immediate_repeat() {
        let mut s = CtcKeywordScorer::new(&[target(0, Tier::Confirmed, &[1, 2], 0.5)], 0);
        // two utterances of [1,2]: second within the 100-frame refractory window
        let frames = vec![
            frame(1, 0.95),
            frame(2, 0.95),
            frame(0, 0.95),
            frame(1, 0.95),
            frame(2, 0.95),
        ];
        assert_eq!(run(&mut s, &frames).len(), 1, "refractory should collapse to one fire");
    }

    #[test]
    fn repeated_adjacent_token_requires_blank_between() {
        // keyword [2,2] must see 2, blank, 2 (CTC cannot collapse them without a
        // blank). A single sustained 2 must NOT fire it.
        let mut s = CtcKeywordScorer::new(&[target(0, Tier::Confirmed, &[2, 2], 0.5)], 0);
        let sustained = vec![frame(2, 0.95), frame(2, 0.95), frame(2, 0.95)];
        assert!(run(&mut s, &sustained).is_empty(), "sustained single token is one 2, not two");

        let mut s2 = CtcKeywordScorer::new(&[target(0, Tier::Confirmed, &[2, 2], 0.5)], 0);
        let separated = vec![frame(2, 0.95), frame(0, 0.9), frame(2, 0.95)];
        assert_eq!(run(&mut s2, &separated).len(), 1, "2,blank,2 is two tokens -> fires");
    }
}
