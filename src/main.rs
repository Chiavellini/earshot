use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};

use earshot::clock::MonotonicNs;
use earshot::config::{self, SourceConfig};
use earshot::decision::DecisionTable;
use earshot::detector::factory as detector_factory;
use earshot::fixture::FixtureSpec;
use earshot::kalshi::auth::{Presigner, Signer};
use earshot::kalshi::book::{run_ws, BookCache};
use earshot::kalshi::executor::{prestage_orders, run_executor, ExecCtx};
use earshot::kalshi::rest::{OrderBodyV2, RestClient, ORDERS_ENDPOINT};
use earshot::kalshi::validator::{assert_non_crossing, m2_live_gate, mention_probe_gate, LiveMarketValidator};
use earshot::kalshi::KalshiEnv;
use earshot::pipeline::preproc::PreprocCfg;
use earshot::pipeline::run_pipeline;
use earshot::risk::{spawn_kill_watcher, RiskConfig, RiskGate};
use earshot::source::replay::ReplaySource;
use earshot::source::twilio_ws::TwilioWsSource;
use earshot::telemetry;
use earshot::types::SourceId;

#[derive(Parser)]
#[command(name = "earshot", about = "Audio-signal latency trader (M2: Kalshi execution)")]
struct Cli {
    /// Emit JSON logs (default: human-readable)
    #[arg(long, global = true)]
    json_logs: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Synthesize the standard test fixture WAV (tone bursts at 2/5/8 s)
    GenFixture {
        #[arg(long, default_value = "fixture.wav")]
        out: PathBuf,
    },
    /// Replay a recorded session through the pipeline and report latency
    Replay {
        #[arg(long)]
        config: PathBuf,
        #[arg(long, default_value = "report.json")]
        report: PathBuf,
        /// Override replay speed (0 = as fast as possible)
        #[arg(long)]
        speed: Option<f32>,
    },
    /// Full pipeline with the execution layer attached. DRY-RUN by default:
    /// orders are computed and journaled but never sent. --i-am-live sends
    /// real orders, gated by allowlist + non-mention + cap ≤ 1¢ (M2 posture).
    Arm {
        #[arg(long)]
        config: PathBuf,
        #[arg(long, default_value = "report.json")]
        report: PathBuf,
        #[arg(long)]
        i_am_live: bool,
        #[arg(long)]
        speed: Option<f32>,
        /// Mention-probe posture: permit Mentions-category markets while
        /// keeping the full probe envelope (1¢ cap, $1 ledger).
        #[arg(long)]
        allow_mention: bool,
    },
    /// Bounded-loss live order probe: 1 contract at 1¢ (worst case $0.01),
    /// non-mention markets only, $1 cumulative ledger cap.
    ProbeOrder {
        #[arg(long)]
        ticker: String,
        #[arg(long, default_value_t = 1)]
        price_cents: u32,
        /// Resting post-only bid instead of IOC (for the kill drill)
        #[arg(long)]
        post_only: bool,
        /// Permit a Mentions-category market — IOC only, and refused unless
        /// the price is strictly below the best ask (guaranteed no fill).
        #[arg(long)]
        allow_mention: bool,
    },
    /// Kill-switch drill: place a resting 1¢ bid, trip state/STOP, verify
    /// cancel-all + disarm in under a second.
    KillDrill {
        #[arg(long)]
        ticker: String,
        /// Create the STOP file automatically after 1 s (else you touch it)
        #[arg(long)]
        auto: bool,
    },
    /// Read-only WS book-cache soak: subscribe and print the book every second.
    Book {
        #[arg(long)]
        ticker: String,
        #[arg(long, default_value_t = 15)]
        secs: u64,
    },
    /// Cancel one specific order by id (ops tool; V2 cancel).
    Cancel {
        #[arg(long)]
        order_id: String,
    },
    /// List resting orders (ops tool; read-only). Optional ticker filter.
    Orders {
        #[arg(long)]
        ticker: Option<String>,
    },
    /// M1 bake-off: run engine × chunk_ms grids over the ground-truth corpus
    /// and score precision/recall/FA-per-hour (and latency with --paced).
    BenchDetector {
        /// Keyword/engine config (sources section is ignored; the corpus
        /// supplies the audio).
        #[arg(long)]
        config: PathBuf,
        /// Corpus root containing manifests/ and the audio they reference.
        #[arg(long, default_value = "corpus")]
        corpus: PathBuf,
        /// Comma-separated: stub,sherpa_kws
        #[arg(long, value_delimiter = ',', default_value = "sherpa_kws")]
        engines: Vec<String>,
        /// Comma-separated chunk sizes in ms.
        #[arg(long, value_delimiter = ',', default_value = "160")]
        chunk_ms: Vec<u32>,
        /// dev = tuning surface; holdout = run ONCE with the frozen config.
        #[arg(long, default_value = "dev")]
        split: String,
        /// Real-time replay for wall-clock onset→emit latency (slow: 1×).
        #[arg(long)]
        paced: bool,
        /// Optional comma-separated call-id filter (subset paced runs).
        #[arg(long, value_delimiter = ',')]
        calls: Option<Vec<String>>,
        #[arg(long, default_value = "bench-report.json")]
        out: PathBuf,
    },
    /// Region/edge RTT probe (docs/INGRESS-BAKEOFF.md §2): TCP-handshake RTT +
    /// loss from this box to each target, to pick the compute region before a
    /// vendor is chosen. Targets are host or host:port (default :443).
    Latprobe {
        #[arg(long, value_delimiter = ',')]
        targets: Vec<String>,
        #[arg(long, default_value_t = 20)]
        samples: u32,
        #[arg(long, default_value_t = 1000)]
        timeout_ms: u64,
        #[arg(long, default_value = "latprobe.json")]
        out: PathBuf,
    },
    /// Ingress bake-off (docs/INGRESS-BAKEOFF.md): capture every configured
    /// [[sources]] leg concurrently against the same call, record PCM, and
    /// report per-leg jitter (p50/p95/p99), stalls, and the cross-stream delta.
    CaptureBakeoff {
        #[arg(long)]
        config: PathBuf,
        /// Safety cap on capture wall-time; replay/hangup ends sooner.
        #[arg(long, default_value_t = 120)]
        secs: u64,
        /// Write per-leg <label>.wav + .jsonl here (non-production tap).
        #[arg(long)]
        record_dir: Option<PathBuf>,
        /// Jitter-buffer sweep value (ms). Wired for the SIP/RTP path where we
        /// own the buffer; a no-op until that adapter lands (Twilio buffers
        /// itself). Logged so the chosen value is on the record.
        #[arg(long)]
        jitter_buffer_ms: Option<u64>,
        #[arg(long, default_value = "capture-bakeoff.json")]
        out: PathBuf,
    },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    telemetry::init_logging(cli.json_logs);
    match run(cli.command) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!("{e}");
            ExitCode::FAILURE
        }
    }
}

type AnyErr = Box<dyn std::error::Error>;

fn run(command: Command) -> Result<(), AnyErr> {
    match command {
        Command::GenFixture { out } => {
            FixtureSpec::standard().write_wav(&out)?;
            println!("wrote standard fixture (10 s, 8 kHz, bursts @ 2/5/8 s) to {}", out.display());
            Ok(())
        }
        Command::Replay { config, report, speed } => {
            let cfg = config::load(&config)?;
            let source = build_source(&cfg, &config, speed)?;
            let detector = detector_factory::build_detector(&cfg)?;
            let table = DecisionTable::from_keywords(&cfg.keywords);
            let preproc_cfg = PreprocCfg { resample_to_hz: cfg.pipeline.resample_to_hz };
            let out = run_pipeline(&cfg.event.id, cfg.pipeline.chunk_ms, preproc_cfg, source, detector, table, None)?;
            print!("\n{}", out.render_table());
            out.write_json(&report)?;
            println!("\nreport written to {}", report.display());
            if out.drops.total() > 0 {
                return Err(format!("{} frames dropped — stalled stage?", out.drops.total()).into());
            }
            Ok(())
        }
        Command::Arm { config, report, i_am_live, speed, allow_mention } => {
            rt()?.block_on(arm(config, report, i_am_live, speed, allow_mention))
        }
        Command::ProbeOrder { ticker, price_cents, post_only, allow_mention } => {
            rt()?.block_on(async {
                probe_order(&ticker, price_cents, post_only, allow_mention, false).await.map(|_| ())
            })
        }
        Command::KillDrill { ticker, auto } => rt()?.block_on(kill_drill(&ticker, auto)),
        Command::Book { ticker, secs } => rt()?.block_on(book_soak(&ticker, secs)),
        Command::Cancel { order_id } => rt()?.block_on(async {
            let (_signer, rest) = connect_live().await?;
            let resp = rest.cancel_order(&order_id).await?;
            println!("cancelled: {}", serde_json::to_string_pretty(&resp)?);
            Ok(())
        }),
        Command::BenchDetector { config, corpus, engines, chunk_ms, split, paced, calls, out } => {
            let engines = engines
                .iter()
                .map(|e| match e.as_str() {
                    "stub" => Ok(earshot::config::EngineKind::Stub),
                    "sherpa_kws" => Ok(earshot::config::EngineKind::SherpaKws),
                    "streaming_ctc" => Ok(earshot::config::EngineKind::StreamingCtc),
                    other => Err(format!("unknown engine {other:?} (stub | sherpa_kws | streaming_ctc)")),
                })
                .collect::<Result<Vec<_>, _>>()?;
            let split = match split.as_str() {
                "dev" => earshot::bench::manifest::Split::Dev,
                "holdout" => earshot::bench::manifest::Split::Holdout,
                other => return Err(format!("unknown split {other:?} (dev | holdout)").into()),
            };
            let report = earshot::bench::run_bench(&earshot::bench::BenchArgs {
                config_path: config,
                corpus,
                engines,
                chunk_ms,
                split,
                paced,
                calls,
            })?;
            print!("\n{}", report.render_table());
            std::fs::write(&out, serde_json::to_string_pretty(&report)?)?;
            println!("\nreport written to {}", out.display());
            Ok(())
        }
        Command::Latprobe { targets, samples, timeout_ms, out } => {
            if targets.is_empty() {
                return Err("latprobe needs --targets host[:port],host[:port],…".into());
            }
            let report = rt()?.block_on(earshot::latprobe::run_latprobe(
                &earshot::latprobe::LatprobeArgs {
                    targets,
                    samples,
                    connect_timeout: Duration::from_millis(timeout_ms),
                },
            ));
            print!("\n{}", report.render_table());
            std::fs::write(&out, serde_json::to_string_pretty(&report)?)?;
            println!("\nreport written to {}", out.display());
            Ok(())
        }
        Command::CaptureBakeoff { config, secs, record_dir, jitter_buffer_ms, out } => {
            let cfg = config::load(&config)?;
            if cfg.sources.len() < 2 {
                return Err("capture-bakeoff needs at least 2 [[sources]] legs".into());
            }
            if let Some(jb) = jitter_buffer_ms {
                tracing::warn!(jitter_buffer_ms = jb, "jitter-buffer sweep is a no-op until the SIP/RTP adapter lands (Twilio owns its buffer)");
            }
            let mut legs = Vec::with_capacity(cfg.sources.len());
            let mut labels = Vec::with_capacity(cfg.sources.len());
            for (i, s) in cfg.sources.iter().enumerate() {
                legs.push(build_leg(s, &config, None, SourceId(i as u16))?);
                labels.push(source_label(s));
            }
            let report = earshot::capture_bakeoff::run_capture_bakeoff(
                earshot::capture_bakeoff::CaptureArgs {
                    legs,
                    labels,
                    max_secs: secs,
                    record_dir,
                },
            )?;
            print!("\n{}", report.render_table());
            std::fs::write(&out, serde_json::to_string_pretty(&report)?)?;
            println!("\nreport written to {}", out.display());
            Ok(())
        }
        Command::Orders { ticker } => rt()?.block_on(async {
            let (_signer, rest) = connect_live().await?;
            let open = rest.open_orders().await?;
            let mut shown = 0;
            for o in &open {
                let t = o["ticker"].as_str().unwrap_or("?");
                if ticker.as_deref().map(|f| f == t).unwrap_or(true) {
                    shown += 1;
                    println!(
                        "{} | {} | side {} | price {} | remaining {} | created {}",
                        o["order_id"].as_str().unwrap_or("?"),
                        t,
                        o["side"].as_str().unwrap_or("?"),
                        o["yes_price"].as_u64().map(|p| format!("{p}¢")).unwrap_or_else(|| o["price_dollars"].as_str().unwrap_or("?").into()),
                        o["remaining_count"].as_u64().map(|c| c.to_string()).unwrap_or_else(|| o["remaining_count_fp"].as_str().unwrap_or("?").into()),
                        o["created_time"].as_str().unwrap_or("?"),
                    );
                }
            }
            println!("({shown} shown / {} open total)", open.len());
            Ok(())
        }),
    }
}

fn rt() -> std::io::Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
}

/// Single construction point for the audio source. One source → the direct
/// path (zero behavior change). Two or more → a `SourceArbiter` (dual dial-in,
/// spec §5.2) wrapping each leg. Replay honors `--speed`; TwilioStream binds
/// its WS listener here so a taken port refuses before anything arms (P3).
fn build_source(
    cfg: &config::EventConfig,
    config_path: &Path,
    speed_override: Option<f32>,
) -> Result<Box<dyn earshot::source::AudioSource>, AnyErr> {
    if cfg.sources.is_empty() {
        return Err("config has no sources".into());
    }
    if cfg.sources.len() == 1 {
        return build_leg(&cfg.sources[0], config_path, speed_override, SourceId(0));
    }
    // Dual dial-in: each configured source becomes an arbiter leg. Distinct
    // SourceIds so per-leg sequence/gap logging stays attributable.
    let mut legs = Vec::with_capacity(cfg.sources.len());
    for (i, s) in cfg.sources.iter().enumerate() {
        legs.push(build_leg(s, config_path, speed_override, SourceId(i as u16))?);
    }
    let params = earshot::source::arbiter::ArbiterParams {
        mode: match cfg.arbiter.mode {
            config::ArbiterMode::Failover => earshot::source::arbiter::ArbiterMode::Failover,
            config::ArbiterMode::TakeFirst => earshot::source::arbiter::ArbiterMode::TakeFirst,
        },
        switch: Duration::from_millis(cfg.arbiter.switch_ms),
        align: Duration::from_millis(cfg.arbiter.align_ms),
    };
    tracing::info!(legs = legs.len(), mode = ?cfg.arbiter.mode, "dual dial-in: arbiter source");
    Ok(Box::new(earshot::source::arbiter::SourceArbiter::new(
        legs,
        params,
        SourceId(0),
    )?))
}

/// Build one concrete leg (used directly for a single source, or per-leg by
/// the arbiter).
fn build_leg(
    src: &SourceConfig,
    config_path: &Path,
    speed_override: Option<f32>,
    source_id: SourceId,
) -> Result<Box<dyn earshot::source::AudioSource>, AnyErr> {
    match src {
        SourceConfig::Replay { path, speed } => {
            let wav_path = {
                let p = PathBuf::from(path);
                if p.is_absolute() {
                    p
                } else {
                    config_path.parent().unwrap_or_else(|| Path::new(".")).join(p)
                }
            };
            Ok(Box::new(ReplaySource::from_wav(
                &wav_path,
                speed_override.unwrap_or(*speed),
                source_id,
            )?))
        }
        SourceConfig::TwilioStream { bind } => {
            let src = TwilioWsSource::bind(bind, source_id)?;
            tracing::info!(addr = %src.local_addr()?, "twilio stream source bound — dial after arming");
            Ok(Box::new(src))
        }
        SourceConfig::Sip { carrier, sip_uri, .. } => Err(format!(
            "raw SIP adapter is gated behind the ingress bake-off (carrier={carrier:?}, \
             sip_uri={sip_uri:?}) — see docs/INGRESS-BAKEOFF.md; use twilio_stream until a \
             winner is chosen"
        )
        .into()),
        SourceConfig::Hls { .. } => Err("HLS adapter not built — use twilio_stream".into()),
    }
}

/// Provider label for the bake-off report / provider→lead table.
fn source_label(s: &SourceConfig) -> String {
    match s {
        SourceConfig::Replay { .. } => "replay".into(),
        SourceConfig::TwilioStream { .. } => "twilio".into(),
        SourceConfig::Sip { carrier, .. } if !carrier.is_empty() => carrier.clone(),
        SourceConfig::Sip { .. } => "sip".into(),
        SourceConfig::Hls { .. } => "hls".into(),
    }
}

/// Shared bring-up for live-connected commands: signer + REST client + skew
/// guard. Read-only until a caller explicitly sends an order.
async fn connect_live() -> Result<(Arc<Signer>, RestClient), AnyErr> {
    let env = KalshiEnv::prod();
    let signer = Arc::new(Signer::from_env()?);
    let rest = RestClient::new(&env, signer.clone())?;
    rest.balance().await.map_err(|e| format!("signed balance self-test failed: {e}"))?;
    let offset = rest.clock_offset_estimate().await?;
    tracing::info!(offset_secs = format!("{offset:+.2}"), "clock offset vs Kalshi");
    if offset.abs() > 10.0 {
        return Err(format!(
            "clock skew {offset:+.1}s vs Kalshi exceeds 10 s guard (window is ±30 s) — fix NTP first"
        )
        .into());
    }
    Ok((signer, rest))
}

async fn wait_for_books(cache: &BookCache, tickers: &[String], timeout: Duration) -> Result<(), AnyErr> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let missing: Vec<&String> = tickers
            .iter()
            .filter(|t| cache.read(t).map(|b| b.as_of.0 == 0).unwrap_or(true))
            .collect();
        if missing.is_empty() {
            return Ok(());
        }
        if tokio::time::Instant::now() > deadline {
            return Err(format!("no WS snapshot for {missing:?} — refusing to arm (P3)").into());
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

async fn arm(
    config_path: PathBuf,
    report_path: PathBuf,
    i_am_live: bool,
    speed: Option<f32>,
    allow_mention: bool,
) -> Result<(), AnyErr> {
    let cfg = config::load(&config_path)?;
    let state_dir = Path::new("state");
    let (signer, rest) = connect_live().await?;

    // Arm-time market validation (rules stored for audit; refuse on any miss).
    let validator = LiveMarketValidator { rest: &rest, state_dir };
    let markets = validator.validate(&cfg).await?;
    tracing::info!(markets = markets.len(), "all keyword markets active; rules stored to state/rules/");

    let gate = Arc::new(RiskGate::new(cfg.risk.clone(), state_dir)?);
    if i_am_live {
        let max_cap = cfg.keywords.iter().map(|k| k.max_price_cents).max().unwrap_or(0);
        if allow_mention {
            mention_probe_gate(&markets, max_cap)?;
        } else {
            m2_live_gate(&markets, max_cap)?;
        }
        if gate.cfg().allowlist_tickers.is_empty() {
            return Err("--i-am-live requires a non-empty [risk].allowlist_tickers".into());
        }
        gate.arm();
        if allow_mention {
            tracing::warn!(
                "ARMED for live orders — MENTION-PROBE posture (allowlist, Mentions permitted, cap ≤ 1¢, $1 ledger)"
            );
        } else {
            tracing::warn!("ARMED for live orders (M2 gates: allowlist, non-mention, cap ≤ 1¢)");
        }
    } else {
        tracing::info!("dry-run: orders will be computed and journaled, never sent");
    }

    // Book cache + WS feed; refuse to proceed without a snapshot per market.
    let tickers: Vec<String> = cfg.keywords.iter().map(|k| k.market_ticker.clone()).collect();
    let cache = BookCache::new(&tickers);
    let ws = tokio::spawn(run_ws(KalshiEnv::prod(), signer.clone(), cache.clone(), tickers.clone()));
    wait_for_books(&cache, &tickers, Duration::from_secs(15)).await?;
    tracing::info!("book snapshots live for all armed markets");

    // Presigned order headers, refreshed every 10 s (measured ±30 s window).
    let presigner = Presigner::new(
        signer.clone(),
        "POST",
        format!("/trade-api/v2{ORDERS_ENDPOINT}"),
    );
    presigner.spawn_refresher(&tokio::runtime::Handle::current());

    // Executor consuming the decision stage's intent channel.
    let haircuts: HashMap<u16, u32> = cfg
        .keywords
        .iter()
        .enumerate()
        .map(|(i, k)| (i as u16, k.rule_risk_haircut_cents as u32))
        .collect();
    let (intent_tx, intent_rx) = tokio::sync::mpsc::channel(64);
    let registry = earshot::kalshi::OrderRegistry::default();
    let ctx = Arc::new(ExecCtx {
        rest: rest.clone(),
        presigner,
        cache: cache.clone(),
        gate: gate.clone(),
        prestaged: prestage_orders(&cfg.event.id, &cfg.keywords),
        haircuts,
        dry_run: !i_am_live,
        registry: registry.clone(),
    });
    let exec_task = tokio::spawn(run_executor(intent_rx, ctx));

    // Kill switch: STOP file or SIGINT → cancel THIS SESSION's orders (never
    // account-wide: unrelated systems' orders share this account) + disarm.
    let rest_for_kill = rest.clone();
    let registry_for_kill = registry.clone();
    let watcher = spawn_kill_watcher(gate.clone(), state_dir.join("STOP"), move || {
        let rest = rest_for_kill.clone();
        let ids = registry_for_kill.ids();
        async move {
            let n = rest.cancel_orders(&ids).await;
            tracing::info!(cancelled = n, of = ids.len(), "kill: session orders cancelled");
        }
    });

    // Audio pipeline runs on its own OS threads (it owns a current-thread
    // runtime for the source, so it must not run inside this runtime).
    let source = build_source(&cfg, &config_path, speed)?;
    let detector = detector_factory::build_detector(&cfg)?;
    let table = DecisionTable::from_keywords(&cfg.keywords);
    let event_id = cfg.event.id.clone();
    let chunk_ms = cfg.pipeline.chunk_ms;
    let preproc_cfg = PreprocCfg { resample_to_hz: cfg.pipeline.resample_to_hz };
    let mut report = tokio::task::spawn_blocking(move || {
        run_pipeline(&event_id, chunk_ms, preproc_cfg, source, detector, table, Some(intent_tx))
    })
    .await??;

    // Pipeline done → intent_tx dropped → executor drains and returns.
    let exec_report = exec_task.await?;
    watcher.abort();
    ws.abort();

    report.edges.extend(exec_report.recorder.snapshot());
    print!("\n{}", report.render_table());
    println!("\norders ({} total, {} LOST_RACE):", exec_report.orders.len(), exec_report.lost_races);
    for o in &exec_report.orders {
        println!(
            "  {} {} x{} @≤{}¢ [{}] {}",
            o.client_order_id,
            o.ticker,
            o.count,
            o.price_cents,
            if o.dry_run { "dry" } else { "LIVE" },
            o.refusal.clone().or(o.order_status.clone()).unwrap_or_default(),
        );
    }

    let mut json = serde_json::to_value(&report)?;
    json["orders"] = serde_json::to_value(&exec_report.orders)?;
    json["lost_races"] = exec_report.lost_races.into();
    std::fs::write(&report_path, serde_json::to_string_pretty(&json)?)?;
    println!("\nreport written to {}", report_path.display());
    Ok(())
}

/// Guard shared by probe-order and kill-drill: this command spends real
/// money, so it is 1 contract at 1¢, ledger-capped. Non-mention markets
/// only, unless `allow_mention` — then IOC-only and strictly below the best
/// ask (guaranteed no fill: proves mention-category order acceptance at $0).
/// Returns the server order id (for the session-scoped kill drill).
async fn probe_order(
    ticker: &str,
    price_cents: u32,
    post_only: bool,
    allow_mention: bool,
    quiet: bool,
) -> Result<Option<String>, AnyErr> {
    if price_cents != 1 {
        return Err("probe-order is hard-limited to --price-cents 1".into());
    }
    let (_signer, rest) = connect_live().await?;

    let market = rest.market(ticker).await?;
    let series_ticker = ticker.split('-').next().unwrap_or(ticker);
    let series = rest.series(series_ticker).await?;
    let category = series["series"]["category"].as_str().unwrap_or("?");
    if category == "Mentions" {
        if !allow_mention {
            return Err(format!("{ticker} is a Mentions market — pass --allow-mention for the no-fill drill").into());
        }
        if post_only {
            return Err("mention probe is IOC-only (a resting bid could fill later)".into());
        }
        // Refuse anything that could actually fill. The REST market object's
        // integer price fields are NULL on prod (pinned 2026-07-12) — prices
        // arrive only as *_dollars fixed-point strings.
        let m = &market["market"];
        let yes_ask = m["yes_ask"]
            .as_u64()
            .or_else(|| {
                m["yes_ask_dollars"]
                    .as_str()
                    .and_then(earshot::kalshi::book::dollars_to_cents)
                    .map(u64::from)
            })
            .filter(|&a| a > 0);
        assert_non_crossing(yes_ask, price_cents)?;
        tracing::info!(ticker, ?yes_ask, "mention probe: price below ask — guaranteed no fill");
    }
    let status = market["market"]["status"].as_str().unwrap_or("?");
    if status != "active" {
        return Err(format!("{ticker} status is {status:?}, not active").into());
    }
    tracing::info!(ticker, category, "probe target validated (active)");

    let gate = RiskGate::new(
        RiskConfig { allowlist_tickers: vec![ticker.to_string()], ..Default::default() },
        Path::new("state"),
    )?;
    gate.arm();
    gate.check_order(ticker, price_cents, 1, 0, 0)?;

    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs();
    let client_order_id =
        earshot::kalshi::rest::deterministic_client_order_id(&format!("probe:{ticker}:{nonce}"));
    let body = if post_only {
        OrderBodyV2::resting_buy_yes(ticker, price_cents, 1, client_order_id.clone())
    } else {
        OrderBodyV2::ioc_buy_yes(ticker, price_cents, 1, client_order_id.clone())
    };

    gate.record_send(price_cents, 1);
    let t0 = MonotonicNs::now();
    let (ack, send_ts) = rest.create_order(&body).await?;
    println!(
        "probe order acked: status_code={} order_status={:?} order_id={:?}",
        ack.status_code, ack.order_status, ack.order_id
    );
    println!(
        "latency: build+sign {} µs, send→ack {} µs",
        send_ts.saturating_since(t0) / 1000,
        ack.ack_ts.saturating_since(send_ts) / 1000
    );
    if !quiet {
        println!("raw ack: {}", serde_json::to_string_pretty(&ack.raw)?);
        println!("ledger spent: {}¢ of 100¢ cap", gate.ledger_spent_cents());
    }
    Ok(ack.order_id)
}

async fn kill_drill(ticker: &str, auto: bool) -> Result<(), AnyErr> {
    let (_signer, rest) = connect_live().await?;
    let open_before = rest.open_orders().await?.len();

    // Place the resting 1¢ bid (goes through every probe guard) and register
    // it as THIS SESSION's order — the only thing the kill switch may touch.
    let order_id = probe_order(ticker, 1, true, false, true)
        .await?
        .ok_or("resting probe order returned no order_id")?;
    let registry = earshot::kalshi::OrderRegistry::default();
    registry.register(order_id.clone());
    println!(
        "resting order {order_id} placed; account has {} other open orders (must be untouched)",
        open_before
    );

    let gate = Arc::new(RiskGate::new(RiskConfig::default(), Path::new("state"))?);
    gate.arm();
    let stop_file = Path::new("state").join("STOP");
    let _ = std::fs::remove_file(&stop_file);

    let rest_for_kill = rest.clone();
    let ids = registry.ids();
    let t0 = std::sync::Arc::new(std::sync::Mutex::new(None::<MonotonicNs>));
    let t0_w = t0.clone();
    let watcher = spawn_kill_watcher(gate.clone(), stop_file.clone(), move || {
        let rest = rest_for_kill.clone();
        let ids = ids.clone();
        let t0 = t0_w.clone();
        async move {
            *t0.lock().unwrap() = Some(MonotonicNs::now());
            let n = rest.cancel_orders(&ids).await;
            tracing::info!(cancelled = n, of = ids.len(), "drill: session orders cancelled");
        }
    });

    if auto {
        tokio::time::sleep(Duration::from_secs(1)).await;
        std::fs::write(&stop_file, "kill drill")?;
        println!("STOP file created");
    } else {
        println!("touch {} to trigger the kill switch…", stop_file.display());
    }

    watcher.await?;
    let elapsed = t0.lock().unwrap().map(|t| MonotonicNs::now().saturating_since(t));
    let open_after = rest.open_orders().await?.len();
    let _ = std::fs::remove_file(&stop_file);

    println!(
        "kill drill: disarmed={} cancel_path_ms={:?} open_before={} open_after={}",
        gate.is_killed(),
        elapsed.map(|ns| ns / 1_000_000),
        open_before,
        open_after,
    );
    if open_after != open_before {
        return Err(format!(
            "kill drill FAILED scope check: expected {open_before} unrelated orders untouched, saw {open_after}"
        )
        .into());
    }
    if !gate.is_killed() {
        return Err("kill drill FAILED: gate not disarmed".into());
    }
    match elapsed.map(|ns| ns / 1_000_000) {
        Some(ms) if ms < 1000 => println!("kill drill PASSED (session order cancelled in {ms} ms, others untouched)"),
        Some(ms) => return Err(format!("kill drill FAILED: cancel took {ms} ms (> 1 s)").into()),
        None => return Err("kill drill FAILED: cancel path never ran".into()),
    }
    Ok(())
}

async fn book_soak(ticker: &str, secs: u64) -> Result<(), AnyErr> {
    let (signer, _rest) = connect_live().await?;
    let tickers = vec![ticker.to_string()];
    let cache = BookCache::new(&tickers);
    let ws = tokio::spawn(run_ws(KalshiEnv::prod(), signer, cache.clone(), tickers));

    for i in 0..secs {
        tokio::time::sleep(Duration::from_secs(1)).await;
        let book = cache.read(ticker);
        match book {
            Some(b) if b.as_of.0 != 0 => println!(
                "t+{i:>3}s  bid {:?}  ask {:?}  levels {}/{}  seq {}  feed_age {} ms",
                b.best_bid(),
                b.best_ask(),
                b.yes_bids.len(),
                b.yes_asks.len(),
                b.seq,
                cache.feed_age_ns() / 1_000_000,
            ),
            _ => println!("t+{i:>3}s  (no snapshot yet)"),
        }
    }
    ws.abort();
    Ok(())
}
