//! `book-tape` — Kalshi live order-book RECORDER (born in the cross-venue
//! program — lane removed 2026-08-12 — now core book evidence; Q-XTAPE).
//!
//! WHY THIS EXISTS (2026-08-03, PLTR). The first real cross-venue join reached
//! "NOT RESOLVABLE" and named its own blocker: `src/kalshi/book.rs` mirrors the
//! WS book into an `ArcSwap` and never persists it, so the only Kalshi time
//! series available to an analysis was the public trades tape — second
//! granularity, against a Polymarket side that barely trades. Every Δt under
//! ~2 s was unreadable. Polymarket's book carries MILLISECOND timestamps and
//! (verified live 2026-08-04) pushes over a WebSocket. Kalshi was the missing
//! half. This is that half.
//!
//! Read-only by construction: this file references no order endpoint, no
//! `OrderRegistry`, and no STOP file. It subscribes to `orderbook_delta` and
//! writes bytes. It cannot place, cancel, or influence an order — which is the
//! point, because it is designed to run DURING an armed event.
//!
//! Output is newline-delimited JSON, UNCOMPRESSED on purpose: a recorder that
//! dies mid-write must leave a readable tape, and an interrupted gzip window is
//! exactly the failure evidence capture cannot afford. Gzip the finished file
//! after a clean exit, never while recording.
//!
//! Rows are VERBATIM wire text plus our clocks. Nothing is parsed here — see
//! `RawFrame` in `src/kalshi/book.rs` for why a parsed tape would be lossy.
//!
//! Usage:
//!   source ~/.kalshi/earshot_env
//!   cargo run --release --example book-tape -- \
//!       --event KXEARNINGSMENTIONAMD-26AUG04 --out state/AMD/kalshi.jsonl --secs 5400
//!   cargo run --release --example book-tape -- --tickers T1,T2 --out /tmp/k.jsonl --secs 60

use std::sync::Arc;

use clap::Parser;
use earshot::kalshi::auth::Signer;
use earshot::kalshi::book::{
    run_ws_url_tapped, BookCache, BookTapeCloseReason, BookTapeOptions, BookTapeSession,
    BookTapeSummary, BookTapeTask, BOOK_TAPE_SCHEMA_VERSION,
};
use earshot::kalshi::rest::RestClient;
use earshot::kalshi::KalshiEnv;

/// Channel depth between the WS reader and the writer. Deep enough that a disk
/// hiccup never costs a frame; bounded so a wedged writer drops (and COUNTS)
/// instead of growing without limit.
const TAP_CAPACITY: usize = 16_384;

#[derive(Parser)]
#[command(
    name = "book-tape",
    about = "Record the Kalshi WS order book to a raw tape"
)]
struct Args {
    /// Event ticker; expands to every market on the board.
    #[arg(long)]
    event: Option<String>,
    /// Explicit comma-separated market tickers (alternative to --event).
    #[arg(long)]
    tickers: Option<String>,
    /// Output path (newline-delimited JSON).
    #[arg(long)]
    out: String,
    /// Record for this many seconds, then close cleanly.
    #[arg(long, default_value_t = 300)]
    secs: u64,
    /// Free-form label recorded in the session header (e.g. the pair_id).
    #[arg(long, default_value = "")]
    label: String,
}

fn git_sha() -> String {
    std::process::Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_else(|| "unknown".into())
}

fn hostname() -> String {
    std::process::Command::new("hostname")
        .arg("-s")
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".into())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();
    let signer = Arc::new(Signer::from_env()?);
    let env = KalshiEnv::prod();
    let rest = RestClient::new(&env, signer.clone())?;

    let tickers: Vec<String> = match (&args.event, &args.tickers) {
        (Some(ev), _) => rest
            .event_markets(ev)
            .await?
            .iter()
            .filter_map(|m| m["ticker"].as_str().map(str::to_string))
            .collect(),
        (None, Some(list)) => list
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect(),
        (None, None) => return Err("need --event or --tickers".into()),
    };
    if tickers.is_empty() {
        return Err(format!("no markets for {:?}", args.event).into());
    }

    // Bound the venue clock BEFORE recording: Kalshi WS frames carry no server
    // timestamp, so the tape's only time axis is our receive stamp. Knowing how
    // far our wall clock sits from the exchange's is what keeps a cross-venue
    // Δt honest rather than merely precise.
    let clock_offset_s = rest.clock_offset_estimate().await.unwrap_or(f64::NAN);

    let cache = BookCache::new(&tickers);
    // The blocking writer exclusively creates and syncs the session row
    // BEFORE it hands us a tap. A rerun into an existing run artifact refuses
    // before the feed starts; it can never truncate prior evidence.
    let (tap, mut tape) = BookTapeTask::start_new(
        &args.out,
        BookTapeSession {
            endpoint: env.ws_url.to_string(),
            event: args.event.clone(),
            label: args.label.clone(),
            tickers: tickers.clone(),
            host: hostname(),
            git_sha: git_sha(),
            clock_offset_s: clock_offset_s.is_finite().then_some(clock_offset_s),
            planned_secs: Some(args.secs),
        },
        cache.clone(),
        TAP_CAPACITY,
        BookTapeOptions::default(),
    )
    .await?;
    let mut writer_failures = tape.subscribe_failures();
    let ws = tokio::spawn(run_ws_url_tapped(
        env.ws_url.to_string(),
        signer,
        cache,
        tickers.clone(),
        Some(tap),
    ));

    eprintln!(
        "book-tape: {} markets → {} (evidence protocol {}, {} s)",
        tickers.len(),
        args.out,
        BOOK_TAPE_SCHEMA_VERSION,
        args.secs
    );

    let deadline = tokio::time::sleep(std::time::Duration::from_secs(args.secs));
    tokio::pin!(deadline);
    // SIGTERM is what launchd, `kill`, and the event timer actually send. The
    // repo's unattended rule is explicit that a STOP-file drill validates the
    // wrong path, so the real signal is the one handled here.
    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    enum Stop {
        Requested(BookTapeCloseReason),
        WriterFailed(String),
        WriterCompleted(Result<BookTapeSummary, String>),
    }
    let stop = tokio::select! {
        _ = &mut deadline => Stop::Requested(BookTapeCloseReason::Deadline),
        _ = sigterm.recv() => Stop::Requested(BookTapeCloseReason::Sigterm),
        _ = tokio::signal::ctrl_c() => Stop::Requested(BookTapeCloseReason::Sigint),
        changed = writer_failures.changed() => {
            let detail = match changed {
                Ok(()) => writer_failures.borrow().clone()
                    .unwrap_or_else(|| "writer failure notification had no detail".to_string()),
                Err(_) => "writer failure channel closed unexpectedly".to_string(),
            };
            Stop::WriterFailed(detail)
        }
        completed = tape.wait() => Stop::WriterCompleted(completed),
    };

    let requested = match stop {
        Stop::Requested(reason) => Some(reason),
        Stop::WriterFailed(error) => {
            ws.abort();
            let _ = ws.await;
            let terminal = tape.wait().await;
            return Err(
                format!("book tape writer failed during capture: {error}; {terminal:?}").into(),
            );
        }
        Stop::WriterCompleted(terminal) => {
            ws.abort();
            let _ = ws.await;
            return Err(format!(
                "book tape writer stopped before the event deadline: {terminal:?}"
            )
            .into());
        }
    };

    if let Some(reason) = requested {
        // Intent first, then producer quiescence: abort + join drops the last
        // FrameTap sender. Only after observing that disconnect may the writer
        // snapshot drops and publish its terminal commit receipt.
        tape.request_close(reason)?;
    }
    ws.abort();
    let _ = ws.await;
    let summary = tape
        .wait()
        .await
        .map_err(|error| format!("book tape join failed: {error}"))?;
    eprintln!(
        "book-tape done: rows={} heartbeats={} dropped={} \
         feed={:?} quiesced={} accounting_complete={} committed={} status={:?} reason={}",
        summary.frame_rows,
        summary.heartbeat_rows,
        summary.tap_dropped,
        summary.terminal_feed_health,
        summary.producer_quiesced,
        summary.terminal_accounting_complete,
        summary.committed,
        summary.status,
        summary.reason
    );
    if summary.tap_dropped > 0 {
        eprintln!(
            "book-tape WARNING: {} frames dropped — tape is INCOMPLETE",
            summary.tap_dropped
        );
    }
    if let Some(error) = summary.writer_error {
        return Err(format!("book tape writer failed: {error}").into());
    }
    if !summary.complete {
        return Err(format!("book tape closed partially: {}", summary.reason).into());
    }
    Ok(())
}
