//! `book-tape` — Kalshi live order-book RECORDER (cross-venue lane, Q-XVENUE).
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
//! exactly the failure this lane cannot afford. `xvenue-autorun.sh` gzips the
//! finished file.
//!
//! Rows are VERBATIM wire text plus our clocks. Nothing is parsed here — see
//! `RawFrame` in `src/kalshi/book.rs` for why a parsed tape would be lossy.
//!
//! Usage:
//!   source ~/.kalshi/earshot_env
//!   cargo run --release --example book-tape -- \
//!       --event KXEARNINGSMENTIONAMD-26AUG04 --out state/xvenue/AMD/kalshi.jsonl --secs 5400
//!   cargo run --release --example book-tape -- --tickers T1,T2 --out /tmp/k.jsonl --secs 60

use std::io::{BufWriter, Write};
use std::sync::atomic::Ordering;
use std::sync::Arc;

use clap::Parser;
use earshot::kalshi::auth::Signer;
use earshot::kalshi::book::{run_ws_url_tapped, BookCache, FrameTap};
use earshot::kalshi::rest::RestClient;
use earshot::kalshi::KalshiEnv;

/// Frozen tape schema. Bumping this REQUIRES updating docs/layers/book-cache.md
/// and scripts/xvenue-normalize.py in the same change — the normalizer refuses
/// a version it does not know rather than guessing.
const XV_SCHEMA: u32 = 1;

/// Channel depth between the WS reader and the writer. Deep enough that a disk
/// hiccup never costs a frame; bounded so a wedged writer drops (and COUNTS)
/// instead of growing without limit.
const TAP_CAPACITY: usize = 16_384;

#[derive(Parser)]
#[command(name = "book-tape", about = "Record the Kalshi WS order book to a raw tape")]
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

fn wall_ns() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
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

    let file = std::fs::File::create(&args.out)?;
    let mut w = BufWriter::new(file);
    let header = serde_json::json!({
        "t": "session",
        "schema_version": XV_SCHEMA,
        "venue": "kalshi",
        "feed_kind": "push",
        "endpoint": env.ws_url,
        "channel": "orderbook_delta",
        "event": args.event,
        "label": args.label,
        "tickers": tickers,
        "host": hostname(),
        "git_sha": git_sha(),
        "clock_offset_s": if clock_offset_s.is_nan() { serde_json::Value::Null }
                          else { serde_json::json!(clock_offset_s) },
        "start_wall_ns": wall_ns(),
        "planned_secs": args.secs,
    });
    writeln!(w, "{header}")?;
    w.flush()?;

    let (tap, mut rx) = FrameTap::new(TAP_CAPACITY);
    let dropped = tap.dropped.clone();
    let cache = BookCache::new(&tickers);
    let ws = tokio::spawn(run_ws_url_tapped(
        env.ws_url.to_string(),
        signer,
        cache,
        tickers.clone(),
        Some(tap),
    ));

    eprintln!(
        "book-tape: {} markets → {} (schema {}, {} s)",
        tickers.len(),
        args.out,
        XV_SCHEMA,
        args.secs
    );

    let deadline = tokio::time::sleep(std::time::Duration::from_secs(args.secs));
    tokio::pin!(deadline);
    // SIGTERM is what launchd, `kill`, and the event timer actually send. The
    // repo's unattended rule is explicit that a STOP-file drill validates the
    // wrong path, so the real signal is the one handled here.
    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut rows: u64 = 0;
    let mut last_beat = std::time::Instant::now();
    let reason;

    loop {
        tokio::select! {
            frame = rx.recv() => {
                let Some(f) = frame else { reason = "feed closed"; break };
                // `text` is embedded as a STRING, not spliced as JSON, so a
                // malformed or unexpected wire frame can never corrupt the tape
                // it is evidence in.
                let row = serde_json::json!({
                    "t": "frame",
                    "wall_ns": f.wall_ns,
                    "mono_ns": f.mono_ns,
                    "raw": f.text,
                });
                writeln!(w, "{row}")?;
                rows += 1;
                if last_beat.elapsed() >= std::time::Duration::from_secs(60) {
                    last_beat = std::time::Instant::now();
                    w.flush()?;
                    eprintln!(
                        "book-tape beat: rows={rows} dropped={}",
                        dropped.load(Ordering::Relaxed)
                    );
                }
            }
            _ = &mut deadline => { reason = "deadline"; break }
            _ = sigterm.recv() => { reason = "SIGTERM"; break }
            _ = tokio::signal::ctrl_c() => { reason = "SIGINT"; break }
        }
    }

    ws.abort();
    let lost = dropped.load(Ordering::Relaxed);
    let trailer = serde_json::json!({
        "t": "close",
        "reason": reason,
        "rows": rows,
        // A truncated tape must be visible as a NUMBER. Silence here would let
        // a partial recording be read as a complete one.
        "dropped": lost,
        "end_wall_ns": wall_ns(),
    });
    writeln!(w, "{trailer}")?;
    w.flush()?;
    eprintln!("book-tape done: rows={rows} dropped={lost} reason={reason}");
    if lost > 0 {
        eprintln!("book-tape WARNING: {lost} frames dropped — tape is INCOMPLETE");
    }
    Ok(())
}
