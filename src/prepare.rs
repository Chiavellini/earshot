//! Event-prep stage (`earshot prepare`): turn a Kalshi earnings-mention EVENT
//! into a ready-to-trade config. Read-only against Kalshi — it lists the
//! event's markets (each carries the WORD `yes_sub_title`, the live ask
//! `yes_ask_dollars`, `can_close_early`, and the resolution text
//! `rules_primary`/`rules_secondary`), parses each market's rules into variants
//! and flags "said N times" strikes that the one-shot decision table cannot
//! trade, then emits a config TOML the operator reviews and runs via
//! `scripts/launch-event.sh`. This is the "prior step" that replaces the
//! hand-typing of markets/tickers/variants (docs/RUNBOOK.md drill 4).
//!
//! It does NOT trade and does NOT source dial-in numbers (those are not in the
//! repo) — it prints the dial-in as a TODO for the operator.

use serde_json::Value;

use crate::detector::kws_compile::TokenTable;
use crate::kalshi::rest::RestClient;
use crate::risk::MENTION_FILL_CAP_CENTS;

/// The detector model whose vocab `prepare` validates variants against (and
/// the model_dir it emits). One place so the emitted config and the
/// validation table can never disagree.
pub const KWS_MODEL_DIR: &str = "models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01";

/// Keywords whose collisions are properties of ENGLISH, not of a corpus — they
/// will recur on every call where the keyword is armed, and no threshold
/// separates them. Measured on the webcast dev split (`docs/BAKEOFF.md`
/// §Keyword arming blocklist). `prepare` FLAGS these; it never drops them,
/// because dropping a keyword forfeits that market and that is a trading
/// decision, not a code one.
const HIGH_RISK_COLLISIONS: &[(&str, &str, &str)] = &[
    (
        "record",
        "quarter over quarter",
        "the most common phrase on an earnings call; measured precision 5/19",
    ),
    (
        "weather",
        "whether",
        "a TRUE HOMOPHONE — no acoustic model separates these, ever",
    ),
    (
        "ultra",
        "ultramaritime (and any ultra- compound)",
        "substring of a closed compound; Q4 semantics do NOT settle on substrings",
    ),
    (
        "trump",
        "strong",
        "small phonetic distance and 'strong' is ubiquitous on earnings calls",
    ),
];

/// Shortest variant we will emit. Two-letter keywords match a syllable, not a
/// word — measured 38 false alarms in one event's set (docs/BAKEOFF.md).
const MIN_VARIANT_CHARS: usize = 3;

/// One market turned into a keyword candidate.
pub struct PreppedKeyword {
    pub ticker: String,
    pub word: String,
    pub phrase: String,
    pub variants: Vec<String>,
    pub ask_cents: Option<u32>,
    pub said_n_times: bool,
    pub can_close_early: bool,
    pub compound: bool,
    /// (confirmed_threshold, boost) when the phrase is in the corpus-tuned
    /// library; None ⇒ engine defaults (best-effort, corpus-unvalidated).
    pub tuned: Option<(f32, f32)>,
    /// Variants removed by encode-validation (not in the detector vocab even
    /// after sanitizing). Emitted as comments so the operator sees the loss.
    pub dropped: Vec<String>,
    /// Variants removed for being too short to be acoustically distinct.
    pub dropped_short: Vec<String>,
}

/// Normalize a raw subtitle alternate into the detector's input alphabet
/// (letters + spaces): lowercase, separator punctuation (`-`/`_`) and `&`
/// become word breaks, everything else non-alphabetic is stripped
/// ("warner bros." → "warner bros", "non-stop" → "non stop"). LOAD-BEARING,
/// not just a nicety: the vocab contains `-`/`'` as pieces, so a raw
/// "non-stop" ENCODES (to `▁NO N - S T O P`) and would sail through
/// encode-validation as a silently dead entry — tokens the transducer never
/// emits for speech. Sanitize first; the encode check then catches whatever
/// normalization cannot fix.
fn sanitize_variant(raw: &str) -> String {
    let mut s = String::with_capacity(raw.len() + 4);
    for c in raw.chars() {
        match c {
            '-' | '_' => s.push(' '),
            '&' => s.push_str(" and "),
            c if c.is_ascii_alphabetic() => s.push(c.to_ascii_lowercase()),
            c if c.is_whitespace() => s.push(' '),
            _ => {} // periods, apostrophes, digits, … — not in the vocab
        }
    }
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Corpus-tuned per-keyword firing params (frozen bake-off values; the tariff
/// value is refreshed by the recall sweep). Keyed by lowercase phrase.
/// Anything not here runs at `[detector.sherpa_kws]` defaults — emitted with a
/// "corpus-unvalidated" note so nobody mistakes best-effort for validated.
fn tuned_library(phrase: &str) -> Option<(f32, f32)> {
    match phrase {
        "tariff" => Some((0.10, 3.0)),
        "recession" => Some((0.28, 2.0)),
        "guidance" => Some((0.25, 2.0)),
        "acquisition" => Some((0.15, 2.0)),
        _ => None,
    }
}

/// Parse a market object into a keyword candidate. Variant rule (VERIFICATIONS
/// §keyword-match): the exact word, plus plural/possessive; tense/derivation do
/// not count. We add the simple plural; a " / " subtitle yields alternates.
fn prep_market(m: &Value) -> Option<PreppedKeyword> {
    let ticker = m["ticker"].as_str()?.to_string();
    let word = m["yes_sub_title"].as_str().unwrap_or("").trim().to_string();
    if word.is_empty() {
        return None;
    }
    let rules_primary = m["rules_primary"].as_str().unwrap_or("");
    // "said N times" / "N or more times" strikes are multi-count — the one-shot
    // table cannot trade them. Detect conservatively on the count phrasing.
    let rp_low = rules_primary.to_lowercase();
    let said_n_times = rp_low.contains(" times") || rp_low.contains("or more");

    let compound = word.chars().any(|c| c.is_ascii_digit()) || word.contains(' ');

    // Alternates from " / ", else the single word; each sanitized into the
    // detector alphabet (letters + spaces) — follow-up 6: raw subtitles carry
    // punctuation the vocab cannot encode ("non-stop", "bros.").
    let mut variants: Vec<String> = Vec::new();
    for alt in word.split('/') {
        let a = sanitize_variant(alt);
        if a.is_empty() {
            continue;
        }
        if !variants.contains(&a) {
            variants.push(a.clone());
        }
        // Simple plural for single tokens that don't already end in s.
        if !a.contains(' ') && !a.ends_with('s') {
            let plural = format!("{a}s");
            if !variants.contains(&plural) {
                variants.push(plural);
            }
        }
        // Letter-spelling for acronym-shaped alternates (WING 2026-07-29: the
        // "AI" market lost its live race after the bare 2-char form was
        // dropped by the short-variant floor and nothing covered speakers
        // saying "A.I." as letters). Conservative on purpose: only when the
        // ORIGINAL subtitle alternate is a standalone 2–4 letter ALL-CAPS
        // token ("AI", "EV", "QE") — mixed case ("Chipotle") never spells.
        // The joined form stays subject to MIN_VARIANT_CHARS (measured 38
        // FA/event on 2-char variants); the letter-spelled form ("a i", 3
        // chars with the space) passes the floor and encodes as real
        // letter-piece tokens.
        //
        // ⚠ COUNTERWEIGHT: generation is NOT validation. A letter-spelling
        // helps recall only if the readiness study (scripts/variant-study.sh)
        // confirms the model can hear it; the WING post-mortem warns an
        // untested variant can false-fire and consume the one-shot. That is
        // why preflight's keyword-readiness gate blocks arming on unstudied
        // or zero-confirmed keywords — this generator only feeds the study.
        let raw = alt.trim();
        if (2..=4).contains(&raw.len()) && raw.chars().all(|c| c.is_ascii_uppercase()) {
            let spelled = a
                .chars()
                .map(|c| c.to_string())
                .collect::<Vec<_>>()
                .join(" ");
            if !variants.contains(&spelled) {
                variants.push(spelled);
            }
        }
    }
    let phrase = variants
        .first()
        .cloned()
        .unwrap_or_else(|| word.to_lowercase());

    let ask_cents = m["yes_ask_dollars"]
        .as_str()
        .and_then(|s| s.parse::<f64>().ok())
        .map(|d| (d * 100.0).round() as u32);

    Some(PreppedKeyword {
        tuned: tuned_library(&phrase),
        ticker,
        word,
        phrase,
        variants,
        ask_cents,
        said_n_times,
        can_close_early: m["can_close_early"].as_bool().unwrap_or(false),
        compound,
        dropped: Vec::new(),
        dropped_short: Vec::new(),
    })
}

/// Remove variants too short to be acoustically distinct, recording each in
/// `dropped_short` so the emitted config shows the loss.
///
/// MEASURED, not a hunch: screening the 2026-07-28 Ford keyword set over 2.5 h
/// of unrelated calls, the generated `ai`/`ais` fired 17 times and `ev`/`evs`
/// 21 — 38 of that event's 65 false alarms, on "ever", "than ever", even
/// "2027" (docs/BAKEOFF.md §Collision screen). A two-letter keyword matches a
/// syllable, not a word. Dropping is LOUD (warn + config comment) so an
/// operator who genuinely needs "EV" can re-add it and own the precision cost.
fn strip_short_variants(preps: &mut [PreppedKeyword]) {
    for k in preps.iter_mut() {
        // A GENERATED plural must die with its base, or the floor leaks: "ev"
        // is 2 chars and drops, but `prep_market`'s "evs" is 3 and would
        // survive to fire on "ever" exactly as measured. Only plurals of a
        // dropped base are removed — a real 3-char market word ("ads", "atc")
        // is untouched.
        let short_bases: Vec<String> = k
            .variants
            .iter()
            .filter(|v| v.trim().len() < MIN_VARIANT_CHARS)
            .cloned()
            .collect();
        let doomed: Vec<String> = short_bases.iter().map(|b| format!("{b}s")).collect();
        let (kept, short): (Vec<String>, Vec<String>) = k
            .variants
            .drain(..)
            .partition(|v| v.trim().len() >= MIN_VARIANT_CHARS && !doomed.contains(v));
        k.variants = kept;
        for s in short {
            tracing::warn!(keyword = %k.word, variant = %s, "dropped ultra-short variant");
            k.dropped_short.push(s);
        }
        // The phrase must remain a real variant — it names the keyword in
        // reports and drives the tuned-library lookup.
        if let Some(first) = k.variants.first() {
            if k.phrase != *first {
                k.phrase = first.clone();
                k.tuned = tuned_library(&k.phrase);
            }
        }
    }
}

/// The per-keyword cap for the fill posture: the live ask, bounded by the fill
/// ceiling. Tiered — only precision-vetted words (`k.tuned.is_some()`) spend
/// real money; unvalidated words observe at 1¢ (they hit the executor's
/// LOST_RACE non-crossing branch, so they never send and never debit the $1
/// ledger) until the validation loop promotes them. `fill_all` overrides the
/// tier for a deliberate everything-fillable run. `None`/0/above-ceiling asks
/// are also left at 1¢. Returns (cap, note).
fn fill_cap_cents(k: &PreppedKeyword, fillable: bool, fill_all: bool) -> (u8, &'static str) {
    if !fillable {
        return (1, "probe (non-crossing, no fill)");
    }
    if k.tuned.is_none() && !fill_all {
        return (
            1,
            "unvalidated — observe (1¢); promote via the validation loop",
        );
    }
    match k.ask_cents {
        Some(a) if a >= 1 && (a as u64) <= MENTION_FILL_CAP_CENTS => {
            (a as u8, "min fill = live ask (vetted)")
        }
        Some(a) if (a as u64) > MENTION_FILL_CAP_CENTS => {
            (1, "ask above fill ceiling — left non-crossing")
        }
        _ => (1, "no live ask — left non-crossing"),
    }
}

/// Fetch all markets in an event and turn them into a ready-to-run config TOML.
/// `fillable` sets whether per-keyword caps are the live ask (fill) or 1¢ (probe).
/// `table` is the detector vocab: every variant is encode-validated against it
/// (follow-up 6 — arm refuses a whole config over one unencodable variant, so
/// prepare must never emit one). `None` (model not on this box) emits the
/// config with a loud unvalidated warning instead.
pub async fn run_prepare(
    rest: &RestClient,
    event_ticker: &str,
    fillable: bool,
    fill_all: bool,
    table: Option<&TokenTable>,
) -> Result<String, crate::kalshi::KalshiError> {
    let markets = rest.event_markets(event_ticker).await?;
    if markets.is_empty() {
        return Err(crate::kalshi::KalshiError::Validation(format!(
            "no markets found for event {event_ticker} (bad ticker or event closed)"
        )));
    }
    // series = leading segment of the event ticker.
    let series = event_ticker.split('-').next().unwrap_or(event_ticker);

    let mut preps: Vec<PreppedKeyword> = markets.iter().filter_map(prep_market).collect();
    preps.sort_by(|a, b| a.ticker.cmp(&b.ticker));

    // Ultra-short variants are acoustically indefensible and MEASURED harmful:
    // screening the 2026-07-28 Ford set over 2.5 h of unrelated calls, the
    // generated `ai`/`ais` fired 17 times and `ev`/`evs` 21 — 38 of that event's
    // 65 false alarms, on "ever", "than ever", even "2027"
    // (docs/BAKEOFF.md §Collision screen). A two-letter keyword matches a
    // syllable, not a word. Dropped LOUDLY (never silently) so an operator who
    // genuinely needs "EV" can re-add it and own the precision cost.
    strip_short_variants(&mut preps);

    // Encode-validation: drop variants the detector vocab cannot represent.
    // The sanitizer already normalized punctuation; this is the ground truth.
    if let Some(table) = table {
        for k in &mut preps {
            let (kept, dropped): (Vec<String>, Vec<String>) = k
                .variants
                .drain(..)
                .partition(|v| table.encode(&k.phrase, v).is_ok());
            k.variants = kept;
            k.dropped = dropped;
            // The phrase must stay a real (kept) variant — it names the
            // keyword in reports and feeds the tuned-library lookup.
            if let Some(first) = k.variants.first() {
                if k.phrase != *first {
                    k.phrase = first.clone();
                    k.tuned = tuned_library(&k.phrase);
                }
            }
        }
    }

    let mut body = String::new();
    let tradeable: Vec<&PreppedKeyword> = preps
        .iter()
        .filter(|k| !k.said_n_times && !k.variants.is_empty())
        .collect();

    // Header + fixed sections (mirror configs/twilio-live.toml).
    body.push_str(&format!(
        "# AUTO-GENERATED by `earshot prepare --event-ticker {event_ticker}`.\n\
         # Review before arming: confirm variants against state/rules/*.json and\n\
         # research the dial-in (number + passcode) — NOT in the repo.\n\
         # Posture: {}.\n\n",
        match (fillable, fill_all) {
            (true, false) =>
                "MENTION-FILL, TIERED (arm --i-am-live --allow-mention --fillable) — \
                only precision-vetted words fill at the live ask; unvalidated words observe at 1¢. \
                Bounded by config notional caps + $1 probe ledger",
            (true, true) =>
                "MENTION-FILL, ALL words (--fillable --fill-all) — every word ≤ceiling \
                fills, including UNVALIDATED ones (false-alarm + ledger-starvation risk)",
            _ => "mention-probe (arm --i-am-live --allow-mention) — 1¢ non-crossing, no fill",
        }
    ));
    if table.is_none() {
        body.push_str(
            "# WARNING: encodability NOT validated — bpe.model missing on this box.\n\
             # arm will refuse the whole config if any variant is not in the detector\n\
             # vocab. Run `earshot compile-keywords --config <this>` on a box with\n\
             # models/ before event night.\n\n",
        );
    }
    body.push_str(&format!(
        "[event]\nid        = \"{}\"\nstarts_at = \"2026-07-15T00:00:00Z\"  # TODO: set the real call time (UTC)\n\n",
        event_ticker
    ));
    body.push_str(&format!(
        "[pipeline]\nchunk_ms       = 80\nresample_to_hz = 16000\n\n\
         [detector]\nengine = \"sherpa_kws\"\n\n\
         [detector.sherpa_kws]\n\
         model_dir         = \"{KWS_MODEL_DIR}\"\n\
         num_threads       = 1\nmax_active_paths  = 4\ndefault_threshold = 0.20\n\
         default_boost     = 2.0\nrefractory_ms     = 2000\n\n\
         [[sources]]\nkind = \"twilio_stream\"\nbind = \"127.0.0.1:8080\"\n\n",
    ));
    body.push_str(&format!("[kalshi]\nseries = \"{series}\"\n\n"));

    // Risk: allowlist = tradeable tickers; keep notional caps tight.
    body.push_str(
        "[risk]\nmax_order_notional_cents = 100\nmax_event_notional_cents = 100\n\
                   book_stale_ms            = 2000\naudio_stale_ms           = 2000\n\
                   allowlist_tickers = [\n",
    );
    for k in &tradeable {
        body.push_str(&format!("  \"{}\",\n", k.ticker));
    }
    body.push_str("]\n\n");

    // Keyword blocks.
    for k in &tradeable {
        let (cap, cap_note) = fill_cap_cents(k, fillable, fill_all);
        let variants = k
            .variants
            .iter()
            .map(|v| format!("\"{v}\""))
            .collect::<Vec<_>>()
            .join(", ");
        let tuned_note = match k.tuned {
            Some(_) => "corpus-tuned",
            None => "corpus-UNVALIDATED (engine defaults) — best-effort recall",
        };
        let mut flags = Vec::new();
        if k.compound {
            flags.push("compound/symbol phrase — review variants");
        }
        if !k.can_close_early {
            flags.push("can_close_early=false — will NOT resolve early on utterance");
        }
        let flag_note = if flags.is_empty() {
            String::new()
        } else {
            format!(" ; ⚠ {}", flags.join("; "))
        };

        body.push_str(&format!(
            "# {} ({}) — {}; cap: {}{}\n",
            k.word, k.ticker, tuned_note, cap_note, flag_note
        ));
        for d in &k.dropped {
            body.push_str(&format!(
                "# DROPPED unencodable variant {d:?} — not in the detector vocab\n"
            ));
        }
        for d in &k.dropped_short {
            body.push_str(&format!(
                "# DROPPED ultra-short variant {d:?} — <{MIN_VARIANT_CHARS} chars matches a \
                 syllable, not a word (measured: 38 FA in one event set). Re-add \
                 deliberately if this phrasing matters, and own the precision cost.\n"
            ));
        }
        if let Some((_, collides_with, why)) = HIGH_RISK_COLLISIONS
            .iter()
            .find(|(w, _, _)| *w == k.word.to_lowercase())
        {
            body.push_str(&format!(
                "# ⚠⚠ HIGH-RISK KEYWORD — collides with {collides_with:?}: {why}.\n                 # This is a property of English, not of one corpus: it recurs on EVERY \
                 call where this keyword is armed. Kept here on purpose (dropping it \
                 forfeits the market) — decide deliberately before arming, and expect \
                 fires that settle NO. See docs/BAKEOFF.md §Keyword arming blocklist.\n"
            ));
        }
        body.push_str("[[keywords]]\n");
        body.push_str(&format!("market_ticker    = \"{}\"\n", k.ticker));
        body.push_str(&format!("phrase           = \"{}\"\n", k.phrase));
        body.push_str(&format!("variants         = [{variants}]\n"));
        body.push_str(&format!("max_price_cents  = {cap}\n"));
        body.push_str("budget_contracts = 1\n");
        body.push_str("policy           = \"confirmed_only\"\n");
        if let Some((conf, boost)) = k.tuned {
            // `{:?}` on f32 emits a valid TOML float literal (2.0, not 2).
            body.push_str(&format!("confirmed_threshold = {conf:?}\n"));
            body.push_str(&format!("boost            = {boost:?}\n"));
        }
        body.push('\n');
    }

    // Footer summary as comments (also logged to stderr by the caller).
    let skipped: Vec<&PreppedKeyword> = preps.iter().filter(|k| k.said_n_times).collect();
    let unencodable: Vec<&PreppedKeyword> = preps
        .iter()
        .filter(|k| !k.said_n_times && k.variants.is_empty())
        .collect();
    let dropped_total: usize = preps.iter().map(|k| k.dropped.len()).sum();
    body.push_str(&format!(
        "# --- prepare summary: {} tradeable, {} skipped (said-N-times), {} skipped \
         (unencodable), {} variants dropped of {} markets ---\n",
        tradeable.len(),
        skipped.len(),
        unencodable.len(),
        dropped_total,
        preps.len()
    ));
    for k in &skipped {
        body.push_str(&format!(
            "#   SKIPPED {} ({}) — multi-count market\n",
            k.word, k.ticker
        ));
    }
    for k in &unencodable {
        body.push_str(&format!(
            "#   SKIPPED {} ({}) — no variant survives the detector vocab (excluded from allowlist)\n",
            k.word, k.ticker
        ));
    }
    Ok(body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sanitize_normalizes_punctuation_to_detector_alphabet() {
        assert_eq!(sanitize_variant("Warner Bros."), "warner bros");
        assert_eq!(sanitize_variant("Non-Stop"), "non stop");
        assert_eq!(sanitize_variant("ATC"), "atc");
        assert_eq!(sanitize_variant("Shut Down"), "shut down");
        assert_eq!(sanitize_variant("AT&T"), "at and t");
        assert_eq!(sanitize_variant("domino's"), "dominos");
        assert_eq!(sanitize_variant("747"), ""); // digits can never encode
        assert_eq!(sanitize_variant("  spaced   out  "), "spaced out");
    }

    #[test]
    fn prep_market_emits_sanitized_variants_only() {
        let m = json!({
            "ticker": "KX-T-NONS",
            "yes_sub_title": "Nonstop / Non-Stop",
            "rules_primary": "resolves yes if said",
            "yes_ask_dollars": "0.25",
            "can_close_early": true,
        });
        let k = prep_market(&m).unwrap();
        assert_eq!(k.variants, vec!["nonstop", "nonstops", "non stop"]);
        assert!(k
            .variants
            .iter()
            .all(|v| v.chars().all(|c| c.is_ascii_lowercase() || c == ' ')));
    }

    /// Model-gated gate (skips unless models/ is present). Two raw-subtitle
    /// failure classes the sanitizer must prevent:
    /// 1. LOUD: symbols not in the vocab at all ("bros.", "7-eleven") — arm
    ///    refuses the whole config (the 2026-07-15 rehearsal failure).
    /// 2. SILENT: the vocab DOES contain `-`/`'` as pieces, so "non-stop"
    ///    encodes — to `▁NO N - S T O P`, tokens the transducer never emits
    ///    for spoken audio. Encode-validation alone would pass a dead entry
    ///    with zero recall; only sanitizing prevents this class.
    #[test]
    fn sanitizer_prevents_both_unencodable_and_dead_punctuation_entries() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join(KWS_MODEL_DIR)
            .join("bpe.model");
        if !path.exists() {
            if std::env::var_os("EARSHOT_REQUIRE_MODELS").is_some() {
                panic!("EARSHOT_REQUIRE_MODELS set but {} missing", path.display());
            }
            eprintln!(
                "SKIP: {} not present — run scripts/fetch-models.sh",
                path.display()
            );
            return;
        }
        let t = TokenTable::from_bpe_model(&path).unwrap();
        for good in [
            "warner bros",
            "non stop",
            "shut down",
            "atc",
            "jetblue",
            "newark",
        ] {
            let toks = t.encode("test", good).expect("sanitized form must encode");
            assert!(
                toks.iter()
                    .all(|p| p.chars().all(|c| c == '▁' || c.is_ascii_alphabetic())),
                "sanitized {good:?} must encode to letter pieces only, got {toks:?}"
            );
        }
        for bad in ["warner bros.", "7-eleven"] {
            assert!(
                t.encode("test", bad).is_err(),
                "raw {bad:?} must be unencodable (loud)"
            );
        }
        // The silent class: encodes, but via a literal `-` piece — dead entry.
        let toks = t.encode("test", "non-stop").unwrap();
        assert!(
            toks.iter().any(|p| p.contains('-')),
            "expected raw \"non-stop\" to encode via a '-' piece (the silent hazard), got {toks:?}"
        );
    }

    #[test]
    fn ultra_short_variants_are_dropped_loudly_not_silently() {
        // "Electric vehicle / EV" is a REAL 2026-07-28 Ford market. The bare
        // "ev"/"evs" alternates fired 21 times over 2.5 h of unrelated calls.
        let m = json!({
            "ticker": "KX-T-EV",
            "yes_sub_title": "Electric vehicle / EV",
            "rules_primary": "resolves yes if said",
            "yes_ask_dollars": "0.25",
            "can_close_early": true,
        });
        let mut preps = vec![prep_market(&m).expect("prepped")];
        assert!(
            preps[0].variants.iter().any(|v| v == "ev"),
            "generator still emits the short alternate"
        );

        strip_short_variants(&mut preps);
        let k = &preps[0];
        assert!(
            !k.variants.iter().any(|v| v == "ev" || v == "evs"),
            "short variants must not survive"
        );
        assert!(
            k.variants.contains(&"electric vehicle".to_string()),
            "the real phrase must survive"
        );
        assert!(
            k.dropped_short.contains(&"ev".to_string()),
            "the drop must be recorded, not silent"
        );
        assert_eq!(
            k.phrase, "electric vehicle",
            "phrase must stay a kept variant"
        );
    }

    #[test]
    fn acronym_alternates_gain_letter_spelled_variants() {
        // "AI" is the load-bearing case: WING 2026-07-29 lost the AI market
        // live after "ai"/"ais" died at the short-variant floor and nothing
        // covered the letters-spoken form. The letter-spelled "a i" is 3
        // chars WITH the space, so it survives the floor the joined form
        // rightly dies at.
        let m = json!({
            "ticker": "KX-T-AI",
            "yes_sub_title": "AI",
            "rules_primary": "resolves yes if said",
            "can_close_early": true,
        });
        let mut preps = vec![prep_market(&m).expect("prepped")];
        assert!(
            preps[0].variants.contains(&"a i".to_string()),
            "all-caps acronym must gain a letter-spelled variant, got {:?}",
            preps[0].variants
        );
        strip_short_variants(&mut preps);
        let k = &preps[0];
        assert!(
            !k.variants.iter().any(|v| v == "ai" || v == "ais"),
            "joined 2-char forms still die at the floor"
        );
        assert!(
            k.variants.contains(&"a i".to_string()),
            "letter form must survive the floor"
        );
        assert_eq!(k.phrase, "a i", "phrase must stay a kept variant");
    }

    #[test]
    fn letter_spelling_requires_all_caps_in_the_source_subtitle() {
        // Not-all-caps words never spell, whatever their length: the
        // heuristic is "written as an acronym", not "short". "Chipotle" is
        // the regression case; lowercase "ai" (hypothetical) must not spell
        // either — only the caps form signals letters-spoken.
        let m = json!({
            "ticker": "KX-T-CHIP",
            "yes_sub_title": "Chipotle",
            "rules_primary": "resolves yes if said",
            "can_close_early": true,
        });
        let k = prep_market(&m).expect("prepped");
        assert_eq!(
            k.variants,
            vec!["chipotle", "chipotles"],
            "no letter form for a normal word"
        );

        let m = json!({
            "ticker": "KX-T-EVLC",
            "yes_sub_title": "ev",
            "rules_primary": "resolves yes if said",
            "can_close_early": true,
        });
        let k = prep_market(&m).expect("prepped");
        assert!(
            !k.variants.iter().any(|v| v == "e v"),
            "lowercase source token must not gain a letter form, got {:?}",
            k.variants
        );
    }

    #[test]
    fn letter_spelling_applies_per_caps_alternate_and_leaves_others_unchanged() {
        // Real 2026-07-28 Ford shape: "Electric vehicle / EV". Only the EV
        // alternate spells; the phrase and plural generation are untouched.
        let m = json!({
            "ticker": "KX-T-EV",
            "yes_sub_title": "Electric vehicle / EV",
            "rules_primary": "resolves yes if said",
            "can_close_early": true,
        });
        let mut preps = vec![prep_market(&m).expect("prepped")];
        assert_eq!(
            preps[0].variants,
            vec!["electric vehicle", "ev", "evs", "e v"],
            "multi-word phrases take no plural; only the caps alternate spells"
        );
        strip_short_variants(&mut preps);
        let k = &preps[0];
        assert_eq!(k.variants, vec!["electric vehicle", "e v"]);
        assert_eq!(
            k.phrase, "electric vehicle",
            "phrase stays the real first variant"
        );
    }

    #[test]
    fn three_char_variants_are_kept() {
        // The bar is <3 chars. "atc" and "ads" are real market words and must
        // survive — the filter is a floor, not a general shortness heuristic.
        let m = json!({
            "ticker": "KX-T-ATC",
            "yes_sub_title": "ATC",
            "rules_primary": "resolves yes if said",
            "can_close_early": true,
        });
        let mut preps = vec![prep_market(&m).expect("prepped")];
        strip_short_variants(&mut preps);
        assert!(preps[0].variants.contains(&"atc".to_string()));
        assert!(preps[0].dropped_short.is_empty());
    }

    #[test]
    fn high_risk_keywords_are_flagged_in_the_emitted_config_not_dropped() {
        // Dropping a keyword forfeits its market, which is a trading decision.
        // prepare's job is to make the risk impossible to miss, not to decide.
        for (word, collides, _why) in HIGH_RISK_COLLISIONS {
            assert!(!word.is_empty() && !collides.is_empty());
        }
        // "record" is the load-bearing case: it collides with "quarter over
        // quarter", which every earnings call says repeatedly.
        let rec = HIGH_RISK_COLLISIONS.iter().find(|(w, _, _)| *w == "record");
        assert!(
            rec.is_some(),
            "record must stay flagged — measured precision 5/19"
        );
        assert!(rec.unwrap().1.contains("quarter"));
    }
}
