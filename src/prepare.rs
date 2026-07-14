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

use crate::kalshi::rest::RestClient;
use crate::risk::MENTION_FILL_CAP_CENTS;

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

    // Alternates from " / ", else the single word; each lowercased.
    let mut variants: Vec<String> = Vec::new();
    for alt in word.split('/') {
        let a = alt.trim().to_lowercase();
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
    }
    let phrase = variants.first().cloned().unwrap_or_else(|| word.to_lowercase());

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
    })
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
        return (1, "unvalidated — observe (1¢); promote via the validation loop");
    }
    match k.ask_cents {
        Some(a) if a >= 1 && (a as u64) <= MENTION_FILL_CAP_CENTS => (a as u8, "min fill = live ask (vetted)"),
        Some(a) if (a as u64) > MENTION_FILL_CAP_CENTS => (1, "ask above fill ceiling — left non-crossing"),
        _ => (1, "no live ask — left non-crossing"),
    }
}

/// Fetch all markets in an event and turn them into a ready-to-run config TOML.
/// `fillable` sets whether per-keyword caps are the live ask (fill) or 1¢ (probe).
pub async fn run_prepare(
    rest: &RestClient,
    event_ticker: &str,
    fillable: bool,
    fill_all: bool,
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

    let mut body = String::new();
    let tradeable: Vec<&PreppedKeyword> = preps.iter().filter(|k| !k.said_n_times).collect();

    // Header + fixed sections (mirror configs/twilio-live.toml).
    body.push_str(&format!(
        "# AUTO-GENERATED by `earshot prepare --event-ticker {event_ticker}`.\n\
         # Review before arming: confirm variants against state/rules/*.json and\n\
         # research the dial-in (number + passcode) — NOT in the repo.\n\
         # Posture: {}.\n\n",
        match (fillable, fill_all) {
            (true, false) => "MENTION-FILL, TIERED (arm --i-am-live --allow-mention --fillable) — \
                only precision-vetted words fill at the live ask; unvalidated words observe at 1¢. \
                Bounded by config notional caps + $1 probe ledger",
            (true, true) => "MENTION-FILL, ALL words (--fillable --fill-all) — every word ≤ceiling \
                fills, including UNVALIDATED ones (false-alarm + ledger-starvation risk)",
            _ => "mention-probe (arm --i-am-live --allow-mention) — 1¢ non-crossing, no fill",
        }
    ));
    body.push_str(&format!(
        "[event]\nid        = \"{}\"\nstarts_at = \"2026-07-15T00:00:00Z\"  # TODO: set the real call time (UTC)\n\n",
        event_ticker
    ));
    body.push_str(
        "[pipeline]\nchunk_ms       = 80\nresample_to_hz = 16000\n\n\
         [detector]\nengine = \"sherpa_kws\"\n\n\
         [detector.sherpa_kws]\n\
         model_dir         = \"models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01\"\n\
         num_threads       = 1\nmax_active_paths  = 4\ndefault_threshold = 0.20\n\
         default_boost     = 2.0\nrefractory_ms     = 2000\n\n\
         [[sources]]\nkind = \"twilio_stream\"\nbind = \"127.0.0.1:8080\"\n\n",
    );
    body.push_str(&format!("[kalshi]\nseries = \"{series}\"\n\n"));

    // Risk: allowlist = tradeable tickers; keep notional caps tight.
    body.push_str("[risk]\nmax_order_notional_cents = 100\nmax_event_notional_cents = 100\n\
                   book_stale_ms            = 2000\naudio_stale_ms           = 2000\n\
                   allowlist_tickers = [\n");
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
    body.push_str(&format!(
        "# --- prepare summary: {} tradeable, {} skipped (said-N-times) of {} markets ---\n",
        tradeable.len(),
        skipped.len(),
        preps.len()
    ));
    for k in &skipped {
        body.push_str(&format!("#   SKIPPED {} ({}) — multi-count market\n", k.word, k.ticker));
    }
    Ok(body)
}
