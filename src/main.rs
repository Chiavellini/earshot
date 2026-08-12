use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};

use earshot::audio_evidence::{
    AudioEvidenceBank, AudioEvidenceCommit, AudioEvidenceConfig, AudioEvidenceHealth,
    AudioEvidenceIdentity, AudioEvidencePartial, AudioEvidencePaths,
};
use earshot::clock::MonotonicNs;
use earshot::config::{self, SourceConfig};
use earshot::decision::{DecisionHealth, DecisionTable};
use earshot::detector::factory as detector_factory;
// `process` is a Detector trait method; detect-explain drives the concrete
// StreamingCtcDetector (it needs `explain()`, which the boxed trait cannot
// expose), so the trait must be in scope for the call to resolve.
use earshot::detector::Detector as _;
use earshot::fixture::FixtureSpec;
use earshot::kalshi::auth::{Presigner, Signer, PRESIGN_REFRESH};
use earshot::kalshi::book::{
    run_ws, run_ws_url_tapped, BookCache, BookTapeCloseReason, BookTapeHandle, BookTapeOptions,
    BookTapeSession, BookTapeSummary, BookTapeTask,
};
use earshot::kalshi::executor::{
    prestage_orders, run_executor, ExecCtx, ExecReport, FixOrderEvidenceHook,
};
use earshot::kalshi::fix::observation_tape::{
    FixObservationTapeCloseReason, FixObservationTapeHandle, FixObservationTapeHealth,
    FixObservationTapeIdentity, FixObservationTapeOptions, FixObservationTapeSummary,
    FixObservationTapeTask,
};
use earshot::kalshi::order_evidence::{
    FillEvidence as DurableFillEvidence, NotAttempted, OrderEvidenceIdentity, OrderEvidenceJournal,
    OrderEvidenceSink, RawEvidence, ReconcileUnresolved, Reconciled, RecoveryAction,
    TransportProtocol,
};
use earshot::kalshi::rest::{OrderBodyV2, RestClient, ORDERS_ENDPOINT};
use earshot::kalshi::transport::OrderTransport;
use earshot::kalshi::validator::{
    assert_non_crossing, m2_live_gate, mention_fill_gate, mention_probe_gate, LiveMarketValidator,
};
use earshot::kalshi::KalshiEnv;
use earshot::pipeline::preproc::PreprocCfg;
use earshot::pipeline::{
    prepare_decision_state, run_pipeline, run_pipeline_admitted, run_pipeline_admitted_with_audio,
    run_pipeline_fanout, run_pipeline_fanout_admitted, run_pipeline_fanout_admitted_with_audio,
    AudioEvidenceForwardReport, DecisionAdmission, PipelineFailure, PipelineFailureKind,
    PipelineWithAudioFailure, PipelineWithAudioReport,
};
use earshot::risk::{
    spawn_coordinated_kill_watcher, spawn_kill_watcher, CoordinatedKill, KillTrigger, RiskConfig,
    RiskGate,
};
use earshot::source::pipe::PipeSource;
use earshot::source::replay::ReplaySource;
use earshot::source::twilio_ws::TwilioWsSource;
use earshot::source::{AudioSource, SourceDescriptor, SourceError, SourceExit};
use earshot::telemetry;
use earshot::telemetry::{ExitReason, ExitRecord, PipelineReport};
use earshot::types::SourceId;

#[derive(Parser)]
#[command(
    name = "earshot",
    about = "Audio-signal latency trader (M2: Kalshi execution)"
)]
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
        /// Report output path. Unset → `<state-dir>/report.json`, so concurrent
        /// events never clobber one shared file (docs/MULTI-EVENT.md).
        #[arg(long)]
        report: Option<PathBuf>,
        #[arg(long)]
        i_am_live: bool,
        #[arg(long)]
        speed: Option<f32>,
        /// Mention-probe posture: permit Mentions-category markets while
        /// keeping the full probe envelope (1¢ cap, $1 ledger).
        #[arg(long)]
        allow_mention: bool,
        /// Mention-FILL posture: allow orders to cross a real ask (cap up to
        /// MENTION_FILL_CAP_CENTS) so they actually fill. Requires --i-am-live
        /// AND --allow-mention. Real spend stays bounded by the config notional
        /// caps and the $1 probe ledger.
        #[arg(long)]
        fillable: bool,
        /// Per-lane event state directory (ledger, STOP, rules/). Unset derives
        /// `state/<config event.id>`. Live mode requires its parent to equal
        /// the canonical repository/RunPlan state root; dry-run may override.
        #[arg(long)]
        state_dir: Option<PathBuf>,
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
    /// Event-prep (the "prior step"): fetch a Kalshi earnings-mention EVENT's
    /// markets + resolution rules and emit a ready-to-run config TOML — the
    /// words, variants, allowlist, and min-fill caps. Read-only on Kalshi;
    /// review the output and add the dial-in before arming.
    Prepare {
        /// Event ticker, e.g. KXEARNINGSMENTIONMS-26JUL15.
        #[arg(long)]
        event_ticker: String,
        /// Output config path. Default: configs/twilio-live-<issuer>.toml
        #[arg(long)]
        out: Option<PathBuf>,
        /// Emit FILL-posture caps (max_price_cents = live ask, bounded by the
        /// fill ceiling) — TIERED: only precision-vetted words fill, the rest
        /// observe at 1¢. Default (no flag): 1¢ non-crossing probe caps.
        #[arg(long)]
        fillable: bool,
        /// Override the tier: make EVERY word ≤ ceiling fillable, including
        /// unvalidated ones (false-alarm + $1-ledger-starvation risk). Only
        /// meaningful with --fillable.
        #[arg(long)]
        fill_all: bool,
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
    /// Print the stable non-secret identity of the credential `arm` would use.
    /// This loads local key material only; it never contacts Kalshi.
    AccountFingerprint {
        /// Apply the arm credential-selection rule for this config. Omit to
        /// fingerprint KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH from env.
        #[arg(long)]
        config: Option<PathBuf>,
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
        /// Comma-separated: stub,sherpa_kws,streaming_ctc
        #[arg(long, value_delimiter = ',', default_value = "sherpa_kws")]
        engines: Vec<String>,
        /// Comma-separated chunk sizes in ms.
        #[arg(long, value_delimiter = ',', default_value = "160")]
        chunk_ms: Vec<u32>,
        /// Comma-separated model dirs to sweep, overriding the engine's
        /// `model_dir`. Omit to use the config's own model. Two exports
        /// compared this way run INTERLEAVED in one invocation, so they share
        /// machine state — the only defensible way to measure a latency delta
        /// on a box with 3.5x run variance (docs/LATENCY-PROOF.md §E0c).
        #[arg(long, value_delimiter = ',')]
        model_dir: Vec<PathBuf>,
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
    /// Print the BPE token-cut for each keyword in a config: the Confirmed
    /// token sequence per variant, and for each `fast_prefixes` candidate the
    /// cut tokens (or why it is rejected). The token-boundary gate of the
    /// prefix-safety method (docs/PREFIX-SAFETY.md); model-parameterized via
    /// the config's model_dir (override with --model-dir to try another model's
    /// tokenizer). Read-only, no model inference — just reads `bpe.model`.
    CompileKeywords {
        /// Event config (its `[[keywords]]` and detector model_dir are read).
        #[arg(long)]
        config: PathBuf,
        /// Override the model directory whose `bpe.model` supplies the
        /// tokenizer. Defaults to the config's sherpa_kws / streaming_ctc
        /// model_dir. Use this to analyze cuts on a different model's vocab.
        #[arg(long)]
        model_dir: Option<PathBuf>,
    },
    /// Arm-day detector liveness gate: build the config's detector exactly as
    /// `arm` would (same engine, model, and ORT dylib env) and push a few
    /// seconds of synthetic audio through it. A wrong-platform or missing
    /// onnxruntime fails/hangs HERE, loudly and before the call window,
    /// instead of silently after "book snapshots live" (Ford 2026-07-28,
    /// RUNBOOK bug 1). Run under `timeout(1)`: a hang exits 124.
    DetectorSmoke {
        #[arg(long)]
        config: PathBuf,
        /// Seconds of synthetic 16 kHz audio to push through the detector.
        #[arg(long, default_value_t = 3)]
        secs: u32,
    },
    /// WHY didn't that keyword fire? Feed a wav through the config's real
    /// streaming_ctc detector and print, per target, the number the firing path
    /// throws away: the PEAK confidence reached (even sub-threshold) and the
    /// per-piece best posterior.
    ///
    /// Built 2026-07-29 for the recall gate: 12 of 52 market words scored ZERO
    /// on clean studio TTS and there was no way to tell which of two OPPOSITE
    /// failures each one was —
    ///   NEAR-MISS      peak just under threshold  ⇒ operating-point problem
    ///   NO-EVIDENCE    a piece the model never emits ⇒ spelling problem, and
    ///                  unreachable by ANY threshold, because confidence is a
    ///                  geometric mean over the pieces
    /// Read-only diagnosis: it cannot change what fires.
    DetectExplain {
        #[arg(long)]
        config: PathBuf,
        /// 16 kHz mono s16 wav (TTS clip from variant-study.sh, or a corpus call).
        #[arg(long)]
        wav: PathBuf,
        /// Override the model dir. ⚠ USE IT. `compile-keywords` resolves the
        /// FIRST detector block and the armed configs keep a [detector.sherpa_kws]
        /// block, so a streaming_ctc config otherwise reports the GIGASPEECH
        /// vocab (preflight-event.sh pins this trap). Default here is the
        /// config's own streaming_ctc.model_dir, which is the correct one.
        #[arg(long)]
        model_dir: Option<PathBuf>,
        /// Only report targets whose phrase contains this (case-insensitive).
        #[arg(long)]
        phrase: Option<String>,
        /// A piece whose best log-posterior is below this is called "never
        /// emitted". ln(0.01) ≈ -4.6, so -6.0 is ~0.25% posterior.
        #[arg(long, default_value_t = -6.0)]
        dead_logp: f32,
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
    /// Bridge-entry admission probe (VERIFICATIONS Q-ADMIT): record the first
    /// N seconds of a bridge's own audio on ONE Twilio leg — no detector, no
    /// config, no [event] — so the recording can be transcribed and the
    /// venue's `admission` field pinned in configs/dial-ins.toml. Signature
    /// material comes from TWILIO_AUTH_TOKEN / TWILIO_STREAM_URL env. Dial
    /// AFTER this binds (scripts/bridge-probe.sh sequences both).
    BridgeProbe {
        /// WS bind address for the Twilio media stream.
        #[arg(long, default_value = "127.0.0.1:8099")]
        bind: String,
        /// Capture wall-time cap; the far end hanging up ends it sooner.
        #[arg(long, default_value_t = 120)]
        secs: u64,
        /// Artifact basename: <label>.wav / .jsonl / .json under --out-dir.
        #[arg(long)]
        label: String,
        #[arg(long, default_value = "corpus/bridge-probes")]
        out_dir: PathBuf,
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
            println!(
                "wrote standard fixture (10 s, 8 kHz, bursts @ 2/5/8 s) to {}",
                out.display()
            );
            Ok(())
        }
        Command::Replay {
            config,
            report,
            speed,
        } => {
            let cfg = config::load(&config)?;
            // Replay never writes listener.ready — the token is a dial gate
            // for live phone events only (arm path).
            let (built, _twilio_port) = build_pipeline_input(&cfg, &config, speed, None)?;
            let table = DecisionTable::from_keywords(&cfg.keywords);
            let preproc_cfg = PreprocCfg {
                resample_to_hz: cfg.pipeline.resample_to_hz,
            };
            let out = match built {
                BuiltPipeline::Single { source, detector } => run_pipeline(
                    &cfg.event.id,
                    cfg.pipeline.chunk_ms,
                    preproc_cfg,
                    source,
                    detector,
                    table,
                    None,
                    cfg.perf,
                )?,
                BuiltPipeline::Fanout { legs } => run_pipeline_fanout(
                    &cfg.event.id,
                    cfg.pipeline.chunk_ms,
                    preproc_cfg,
                    legs,
                    table,
                    None,
                    cfg.perf,
                )?,
            };
            print!("\n{}", out.render_table());
            out.write_json(&report)?;
            println!("\nreport written to {}", report.display());
            if out.drops.total() > 0 {
                return Err(
                    format!("{} frames dropped — stalled stage?", out.drops.total()).into(),
                );
            }
            Ok(())
        }
        Command::Arm {
            config,
            report,
            i_am_live,
            speed,
            allow_mention,
            fillable,
            state_dir,
        } => rt()?.block_on(arm(
            config,
            report,
            i_am_live,
            speed,
            allow_mention,
            fillable,
            state_dir,
        )),
        Command::ProbeOrder {
            ticker,
            price_cents,
            post_only,
            allow_mention,
        } => rt()?.block_on(async {
            probe_order(&ticker, price_cents, post_only, allow_mention, false)
                .await
                .map(|_| ())
        }),
        Command::Prepare {
            event_ticker,
            out,
            fillable,
            fill_all,
        } => rt()?.block_on(async {
            let (_signer, rest) = connect_live().await?;
            // Encode-validation table (follow-up 6). Missing model ⇒ emit the
            // config anyway, loudly unvalidated (prepare must work anywhere).
            let bpe = PathBuf::from(earshot::prepare::KWS_MODEL_DIR).join("bpe.model");
            let table = match earshot::detector::kws_compile::TokenTable::from_bpe_model(&bpe) {
                Ok(t) => Some(t),
                Err(e) => {
                    eprintln!(
                        "WARNING: variants NOT encode-validated ({e}) — arm may refuse this \
                         config; run compile-keywords on a box with models/"
                    );
                    None
                }
            };
            let toml = earshot::prepare::run_prepare(
                &rest,
                &event_ticker,
                fillable,
                fill_all,
                table.as_ref(),
            )
            .await?;
            let issuer = event_ticker
                .split('-')
                .next()
                .unwrap_or("event")
                .to_lowercase()
                .replace("kxearningsmention", "");
            let out =
                out.unwrap_or_else(|| PathBuf::from(format!("configs/twilio-live-{issuer}.toml")));
            std::fs::write(&out, &toml)?;
            println!("wrote {} ({} bytes)", out.display(), toml.len());
            Ok(())
        }),
        Command::KillDrill { ticker, auto } => rt()?.block_on(kill_drill(&ticker, auto)),
        Command::Book { ticker, secs } => rt()?.block_on(book_soak(&ticker, secs)),
        Command::Cancel { order_id } => rt()?.block_on(async {
            let (_signer, rest) = connect_live().await?;
            let resp = rest.cancel_order(&order_id).await?;
            println!("cancelled: {}", serde_json::to_string_pretty(&resp)?);
            Ok(())
        }),
        Command::AccountFingerprint { config } => {
            let signer = match config {
                Some(path) => {
                    let cfg = config::load(&path)?;
                    load_signer_for(&cfg, &path)?
                }
                None => Signer::from_env()?,
            };
            println!("{}", signer.account_fingerprint());
            Ok(())
        }
        Command::BenchDetector {
            config,
            corpus,
            engines,
            chunk_ms,
            model_dir,
            split,
            paced,
            calls,
            out,
        } => {
            let engines = engines
                .iter()
                .map(|e| match e.as_str() {
                    "stub" => Ok(earshot::config::EngineKind::Stub),
                    "sherpa_kws" => Ok(earshot::config::EngineKind::SherpaKws),
                    "streaming_ctc" => Ok(earshot::config::EngineKind::StreamingCtc),
                    other => Err(format!(
                        "unknown engine {other:?} (stub | sherpa_kws | streaming_ctc)"
                    )),
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
                model_dirs: model_dir,
            })?;
            print!("\n{}", report.render_table());
            std::fs::write(&out, serde_json::to_string_pretty(&report)?)?;
            println!("\nreport written to {}", out.display());
            Ok(())
        }
        Command::CompileKeywords { config, model_dir } => {
            use earshot::detector::kws_compile::{prefix_cut, TokenTable};
            let cfg = config::load(&config)?;
            let dir = model_dir
                .or_else(|| cfg.detector.sherpa_kws.as_ref().map(|c| c.model_dir.clone()))
                .or_else(|| cfg.detector.streaming_ctc.as_ref().map(|c| c.model_dir.clone()))
                .ok_or("no model_dir: pass --model-dir or use a config with a sherpa_kws/streaming_ctc section")?;
            let table = TokenTable::from_bpe_model(&dir.join("bpe.model"))?;
            println!(
                "compile-keywords  config={}  bpe={}\n",
                config.display(),
                dir.join("bpe.model").display()
            );
            for kw in &cfg.keywords {
                println!(
                    "{}  ({}, policy {:?})",
                    kw.phrase, kw.market_ticker, kw.policy
                );
                for variant in &kw.variants {
                    let toks = table.encode(&kw.phrase, variant)?;
                    println!(
                        "  confirmed [{}]: {}  ({} tokens)",
                        variant,
                        toks.join(" "),
                        toks.len()
                    );
                }
                if kw.fast_prefixes.is_empty() {
                    println!("  (no fast_prefixes — confirmed-only)");
                }
                for prefix in &kw.fast_prefixes {
                    match kw
                        .variants
                        .iter()
                        .find(|v| v.to_lowercase().starts_with(&prefix.to_lowercase()))
                    {
                        None => println!(
                            "  fast [{prefix}]: REJECTED — no variant starts with this prefix"
                        ),
                        Some(variant) => {
                            let full = table.encode(&kw.phrase, variant)?;
                            let cut = prefix_cut(&full, prefix);
                            if cut >= full.len() {
                                println!(
                                    "  fast [{prefix}]: REJECTED — not shorter than full [{variant}] = {} ({} tokens)",
                                    full.join(" "),
                                    full.len()
                                );
                            } else {
                                println!(
                                    "  fast [{prefix}]: {}  (cut {}/{} tokens of [{variant}])",
                                    full[..cut].join(" "),
                                    cut,
                                    full.len()
                                );
                            }
                        }
                    }
                }
                println!();
            }
            Ok(())
        }
        Command::DetectorSmoke { config, secs } => {
            let cfg = config::load(&config)?;
            let t0 = std::time::Instant::now();
            let mut det = detector_factory::build_detector(&cfg)?;
            let build_ms = t0.elapsed().as_millis();
            let chunk_samples = 16_000 * cfg.pipeline.chunk_ms.max(10) as usize / 1000;
            // A swept tone, not silence: exercises the acoustic frontend while
            // guaranteeing no keyword can fire on it.
            let pcm: Vec<i16> = (0..16_000 * secs as usize)
                .map(|i| {
                    let t = i as f32 / 16_000.0;
                    ((2.0 * std::f32::consts::PI * (200.0 + 60.0 * t) * t).sin() * 8000.0) as i16
                })
                .collect();
            let mut chunks = 0u64;
            for (seq, window) in pcm.chunks(chunk_samples).enumerate() {
                let now = MonotonicNs::now();
                let chunk = earshot::types::AudioChunk {
                    pcm: Arc::from(window),
                    sample_rate_hz: 16_000,
                    first_capture_ts: now,
                    last_capture_ts: now,
                    first_preproc_ts: now,
                    chunk_ts: now,
                    source: SourceId(0),
                    seq: seq as u64,
                };
                let _ = det.process(&chunk);
                chunks += 1;
            }
            if chunks == 0 {
                return Err("detector-smoke: zero chunks processed".into());
            }
            println!(
                "detector-smoke OK engine={:?} build_ms={build_ms} chunks={chunks} total_ms={}",
                cfg.detector.engine,
                t0.elapsed().as_millis()
            );
            Ok(())
        }
        Command::DetectExplain {
            config,
            wav,
            model_dir,
            phrase,
            dead_logp,
        } => {
            let cfg = config::load(&config)?;
            let sc = cfg
                .detector
                .streaming_ctc
                .as_ref()
                .ok_or("detect-explain needs a [detector.streaming_ctc] block")?;
            let mdir = model_dir.unwrap_or_else(|| sc.model_dir.clone());

            // id -> piece, so a dead piece can be NAMED rather than numbered.
            let mut id_to_piece: std::collections::HashMap<u32, String> = Default::default();
            for line in std::fs::read_to_string(mdir.join("tokens.txt"))?.lines() {
                let mut it = line.rsplitn(2, ' ');
                if let (Some(id), Some(p)) = (it.next(), it.next()) {
                    if let Ok(id) = id.trim().parse::<u32>() {
                        id_to_piece.insert(id, p.to_string());
                    }
                }
            }

            // StreamingCtcConfig is not Clone, so build one explicitly with the
            // model dir overridden and every other knob taken verbatim from the
            // config — the point of this tool is to reproduce the ARMED
            // operating point, so nothing else may drift.
            let scfg = earshot::config::StreamingCtcConfig {
                model_dir: mdir.clone(),
                num_threads: sc.num_threads,
                blank_id: sc.blank_id,
                default_threshold: sc.default_threshold,
                refractory_ms: sc.refractory_ms,
                precision: sc.precision.clone(),
            };
            let mut det =
                earshot::detector::streaming_ctc::StreamingCtcDetector::new(&scfg, &cfg.keywords)?;

            let mut r = hound::WavReader::open(&wav)?;
            let spec = r.spec();
            if spec.channels != 1 || spec.sample_rate != 16_000 {
                return Err(format!(
                    "{}: need 16 kHz mono, got {} Hz / {} ch",
                    wav.display(),
                    spec.sample_rate,
                    spec.channels
                )
                .into());
            }
            let pcm: Vec<i16> = match spec.sample_format {
                hound::SampleFormat::Int => r.samples::<i16>().map(|s| s.unwrap_or(0)).collect(),
                hound::SampleFormat::Float => r
                    .samples::<f32>()
                    .map(|s| (s.unwrap_or(0.0) * 32767.0) as i16)
                    .collect(),
            };
            let chunk_samples = 16_000 * cfg.pipeline.chunk_ms.max(10) as usize / 1000;
            let mut fired_n = 0usize;
            for (seq, window) in pcm.chunks(chunk_samples).enumerate() {
                let now = MonotonicNs::now();
                let chunk = earshot::types::AudioChunk {
                    pcm: Arc::from(window),
                    sample_rate_hz: 16_000,
                    first_capture_ts: now,
                    last_capture_ts: now,
                    first_preproc_ts: now,
                    chunk_ts: now,
                    source: SourceId(0),
                    seq: seq as u64,
                };
                fired_n += det.process(&chunk).len();
            }

            println!(
                "detect-explain  wav={}  {:.2}s  model={}",
                wav.display(),
                pcm.len() as f32 / 16_000.0,
                mdir.display()
            );
            println!("keywords={} fires={fired_n}\n", cfg.keywords.len());

            let filt = phrase.map(|p| p.to_lowercase());
            for ex in det.explain() {
                let kw = match cfg.keywords.get(ex.keyword.0 as usize) {
                    Some(k) => k,
                    None => continue,
                };
                if let Some(f) = &filt {
                    if !kw.phrase.to_lowercase().contains(f) {
                        continue;
                    }
                }
                let pieces: Vec<String> = ex
                    .tokens
                    .iter()
                    .map(|t| {
                        id_to_piece
                            .get(t)
                            .cloned()
                            .unwrap_or_else(|| format!("<{t}>"))
                    })
                    .collect();
                // The smallest per-piece posterior is what a geometric mean is
                // dominated by, so name it explicitly.
                let worst = ex
                    .piece_best_logp
                    .iter()
                    .enumerate()
                    .min_by(|a, b| a.1.partial_cmp(b.1).unwrap_or(std::cmp::Ordering::Equal));
                let verdict = if ex.fired {
                    "FIRED".to_string()
                } else if let Some((i, &lp)) = worst {
                    if lp < dead_logp {
                        format!("NO-EVIDENCE (piece {:?} best logP {:.2})", pieces[i], lp)
                    } else if ex.peak_confidence >= ex.threshold * 0.5 {
                        format!(
                            "NEAR-MISS (peak {:.4} vs threshold {:.4}, short by {:.4})",
                            ex.peak_confidence,
                            ex.threshold,
                            ex.threshold - ex.peak_confidence
                        )
                    } else {
                        format!(
                            "WEAK (peak {:.4} vs threshold {:.4})",
                            ex.peak_confidence, ex.threshold
                        )
                    }
                } else {
                    "NO-TARGET".to_string()
                };
                println!(
                    "{:<24} {:<9} k={} thr={:.3} peak={:.4}@f{}  {}",
                    kw.phrase,
                    format!("{:?}", ex.tier),
                    ex.k,
                    ex.threshold,
                    ex.peak_confidence,
                    ex.peak_frame,
                    verdict
                );
                for (i, p) in pieces.iter().enumerate() {
                    let lp = ex.piece_best_logp[i];
                    let mark = if lp < dead_logp {
                        " <-- never emitted"
                    } else {
                        ""
                    };
                    println!(
                        "    piece {i}: {:<10} best logP {:>7.2}  (p={:.4}) @f{}{}",
                        p,
                        lp,
                        lp.exp(),
                        ex.piece_best_frame[i],
                        mark
                    );
                }
                println!();
            }
            Ok(())
        }
        Command::Latprobe {
            targets,
            samples,
            timeout_ms,
            out,
        } => {
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
        Command::CaptureBakeoff {
            config,
            secs,
            record_dir,
            jitter_buffer_ms,
            out,
        } => {
            let cfg = config::load(&config)?;
            if cfg.sources.len() < 2 {
                return Err("capture-bakeoff needs at least 2 [[sources]] legs".into());
            }
            if let Some(jb) = jitter_buffer_ms {
                tracing::info!(
                    jitter_buffer_ms = jb,
                    "jitter-buffer sweep applies to SIP/RTP legs (Twilio owns its own buffer)"
                );
            }
            let mut legs = Vec::with_capacity(cfg.sources.len());
            let mut labels = Vec::with_capacity(cfg.sources.len());
            let twilio_auth = resolve_twilio_auth(&cfg, &config)?;
            for (i, s) in cfg.sources.iter().enumerate() {
                legs.push(build_leg(
                    s,
                    &config,
                    None,
                    SourceId(i as u16),
                    twilio_auth.as_ref(),
                    jitter_buffer_ms.map(|v| v as u32),
                    &mut None,
                )?);
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
        Command::BridgeProbe {
            bind,
            secs,
            label,
            out_dir,
        } => {
            let src = TwilioWsSource::bind(&bind, SourceId(0))?;
            tracing::info!(addr = %src.local_addr()?, "bridge probe listening — dial now");
            let report = earshot::capture_bakeoff::run_bridge_probe(
                earshot::capture_bakeoff::BridgeProbeArgs {
                    leg: Box::new(src),
                    label: label.clone(),
                    max_secs: secs,
                    out_dir: out_dir.clone(),
                },
            )?;
            let json_path = std::path::Path::new(&report.wav_path).with_extension("json");
            earshot::fsio::write_atomic(
                &json_path,
                serde_json::to_string_pretty(&report)?.as_bytes(),
            )?;
            println!(
                "bridge probe {}: held {:.1}s ({} frames, {} stalls)\n  wav:  {}\n  json: {}",
                report.label,
                report.held_ms as f64 / 1000.0,
                report.frames,
                report.stalls_over_100ms,
                report.wav_path,
                json_path.display(),
            );
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
                        o["yes_price"]
                            .as_u64()
                            .map(|p| format!("{p}¢"))
                            .unwrap_or_else(|| o["price_dollars"].as_str().unwrap_or("?").into()),
                        o["remaining_count"]
                            .as_u64()
                            .map(|c| c.to_string())
                            .unwrap_or_else(|| o["remaining_count_fp"]
                                .as_str()
                                .unwrap_or("?")
                                .into()),
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

/// Source-neutral cancellation shared by every concrete ingress leg in one
/// runtime session.  The atomic preserves cancellation that happens before a
/// source starts polling; `Notify` wakes every already-running source runtime.
#[derive(Clone, Default)]
struct SourceCancellation {
    inner: Arc<SourceCancellationInner>,
}

#[derive(Default)]
struct SourceCancellationInner {
    cancelled: AtomicBool,
    notify: tokio::sync::Notify,
}

impl SourceCancellation {
    fn cancel(&self) {
        self.inner.cancelled.store(true, Ordering::Release);
        self.inner.notify.notify_waiters();
    }

    fn is_cancelled(&self) -> bool {
        self.inner.cancelled.load(Ordering::Acquire)
    }

    async fn cancelled(&self) {
        loop {
            if self.is_cancelled() {
                return;
            }
            let notified = self.inner.notify.notified();
            if self.is_cancelled() {
                return;
            }
            notified.await;
        }
    }
}

/// Wrap the concrete adapter, not an outer SourceArbiter.  Arbiters spawn
/// inner Tokio tasks; wrapping only the outer future could detach those tasks
/// on cancellation.  Wrapping each leg makes them all close their ring sender
/// and lets the arbiter/pipeline drain normally.
struct CancellableAudioSource {
    inner: Box<dyn AudioSource>,
    cancellation: SourceCancellation,
}

#[async_trait::async_trait]
impl AudioSource for CancellableAudioSource {
    fn descriptor(&self) -> SourceDescriptor {
        self.inner.descriptor()
    }

    async fn run(
        self: Box<Self>,
        tx: earshot::ring::RingSender<earshot::types::AudioFrame>,
    ) -> Result<SourceExit, SourceError> {
        let CancellableAudioSource {
            inner,
            cancellation,
        } = *self;
        tokio::select! {
            biased;
            _ = cancellation.cancelled() => Ok(SourceExit::Cancelled),
            outcome = inner.run(tx) => outcome,
        }
    }
}

fn with_source_cancellation(
    source: Box<dyn AudioSource>,
    cancellation: Option<&SourceCancellation>,
) -> Box<dyn AudioSource> {
    match cancellation {
        Some(cancellation) => Box::new(CancellableAudioSource {
            inner: source,
            cancellation: cancellation.clone(),
        }),
        None => source,
    }
}

/// Single construction point for the audio source. One source → the direct
/// path (zero behavior change). Two or more → a `SourceArbiter` (dual dial-in,
/// spec §5.2) wrapping each leg. Replay honors `--speed`; TwilioStream binds
/// its WS listener here so a taken port refuses before anything arms (P3).
fn build_source(
    cfg: &config::EventConfig,
    config_path: &Path,
    speed_override: Option<f32>,
    twilio_port: &mut Option<u16>,
    cancellation: Option<&SourceCancellation>,
) -> Result<Box<dyn earshot::source::AudioSource>, AnyErr> {
    if cfg.sources.is_empty() {
        return Err("config has no sources".into());
    }
    // Resolve per-account Twilio signature material once (auth token + pinned
    // stream URL) so every Twilio leg is bound to THIS config's account.
    let twilio_auth = resolve_twilio_auth(cfg, config_path)?;
    if cfg.sources.len() == 1 {
        let source = build_leg(
            &cfg.sources[0],
            config_path,
            speed_override,
            SourceId(0),
            twilio_auth.as_ref(),
            None,
            twilio_port,
        )?;
        return Ok(with_source_cancellation(source, cancellation));
    }
    // Dual dial-in: each configured source becomes an arbiter leg. Distinct
    // SourceIds so per-leg sequence/gap logging stays attributable.
    let mut legs = Vec::with_capacity(cfg.sources.len());
    for (i, s) in cfg.sources.iter().enumerate() {
        let source = build_leg(
            s,
            config_path,
            speed_override,
            SourceId(i as u16),
            twilio_auth.as_ref(),
            None,
            twilio_port,
        )?;
        legs.push(with_source_cancellation(source, cancellation));
    }
    let params = earshot::source::arbiter::ArbiterParams {
        mode: match cfg.arbiter.mode {
            config::ArbiterMode::Failover => earshot::source::arbiter::ArbiterMode::Failover,
            config::ArbiterMode::TakeFirst => earshot::source::arbiter::ArbiterMode::TakeFirst,
            // Fanout never collapses to one stream; callers route through
            // build_pipeline_input before ever reaching this point.
            config::ArbiterMode::Fanout => {
                return Err("fanout mode has no arbiter source — use build_pipeline_input".into())
            }
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

/// Read this config's Twilio auth token + pinned stream URL, if a `[twilio]`
/// section is present. Returns `(auth_token, stream_url)`. Absent → `None`,
/// and the Twilio source falls back to the `TWILIO_*` env pair.
fn resolve_twilio_auth(
    cfg: &config::EventConfig,
    config_path: &Path,
) -> Result<Option<(String, String)>, AnyErr> {
    let Some(tw) = &cfg.twilio else {
        return Ok(None);
    };
    let token_path = resolve_relative(config_path, &tw.auth_token_path);
    let token = std::fs::read_to_string(&token_path)
        .map_err(|e| {
            format!(
                "read [twilio].auth_token_path {}: {e}",
                token_path.display()
            )
        })?
        .trim()
        .to_string();
    if token.is_empty() {
        return Err(format!("[twilio].auth_token_path {} is empty", token_path.display()).into());
    }
    Ok(Some((token, tw.stream_url.clone())))
}

/// Build one concrete leg (used directly for a single source, or per-leg by
/// the arbiter). `twilio_auth` is this config's per-account signature material.
/// `twilio_port` surfaces the first Twilio leg's actually-bound listener port
/// (an ephemeral `:0` bind resolves here) for the arm-path `listener.ready`
/// token; non-Twilio legs leave it untouched.
fn build_leg(
    src: &SourceConfig,
    config_path: &Path,
    speed_override: Option<f32>,
    source_id: SourceId,
    twilio_auth: Option<&(String, String)>,
    jitter_buffer_ms: Option<u32>,
    twilio_port: &mut Option<u16>,
) -> Result<Box<dyn earshot::source::AudioSource>, AnyErr> {
    match src {
        SourceConfig::Replay { path, speed } => {
            let wav_path = resolve_relative(config_path, path);
            Ok(Box::new(ReplaySource::from_wav(
                &wav_path,
                speed_override.unwrap_or(*speed),
                source_id,
            )?))
        }
        SourceConfig::TwilioStream {
            bind,
            idle_timeout_secs,
            accept_timeout_secs,
            reaccept_window_secs,
            max_reconnects,
        } => {
            let mut src = TwilioWsSource::bind(bind, source_id)?;
            if twilio_port.is_none() {
                *twilio_port = Some(src.local_addr()?.port());
            }
            if let Some(v) = idle_timeout_secs {
                src.idle_timeout = std::time::Duration::from_secs(*v);
            }
            if let Some(v) = accept_timeout_secs {
                src.accept_timeout = std::time::Duration::from_secs(*v);
            }
            if let Some(v) = reaccept_window_secs {
                src.reaccept_window = std::time::Duration::from_secs(*v);
            }
            if let Some(v) = max_reconnects {
                src.max_reconnects = *v;
            }
            if let Some((token, url)) = twilio_auth {
                // Config-bound account: pin signature material so a stream signed
                // by the wrong account's token is rejected (403), and the signed
                // URL base is never reconstructed from the client Host header.
                src = src.with_auth(token.clone(), Some(url.clone()));
                tracing::info!(stream_url = %url, "twilio stream source bound to config account");
            } else {
                tracing::info!(addr = %src.local_addr()?, "twilio stream source bound (env signature material) — dial after arming");
            }
            Ok(Box::new(src))
        }
        SourceConfig::Sip {
            carrier,
            sip_uri,
            rtp_bind,
            ..
        } => {
            // Media plane: when an RTP bind address is configured, construct the
            // RTP-receive source (carrier-independent). The SIP signaling that
            // points a carrier's media here is separate and gated on the bake-off.
            if !rtp_bind.is_empty() {
                let jitter = jitter_buffer_ms.unwrap_or(40); // docs/INGRESS-BAKEOFF.md start value
                tracing::info!(rtp_bind = %rtp_bind, jitter_ms = jitter, carrier = %carrier, "sip/rtp media-plane source");
                Ok(Box::new(earshot::source::sip::SipRtpSource::bind(
                    rtp_bind, source_id, jitter,
                )?))
            } else {
                Err(format!(
                    "SIP signaling is gated behind the ingress bake-off (carrier={carrier:?}, \
                     sip_uri={sip_uri:?}); set [[sources]].rtp_bind to run the media plane against \
                     a gateway/loopback — see docs/INGRESS-BAKEOFF.md §5"
                )
                .into())
            }
        }
        // Webcast lane (adapter #4, source::pipe): ffmpeg/subprocess decodes
        // to raw PCM. Tens of seconds behind the bridge — capture/observe
        // posture; see docs/history/RUNBOOK.md webcast-lane notes.
        SourceConfig::Hls { url } => Ok(Box::new(PipeSource::hls(url, source_id)?)),
        SourceConfig::Pipe {
            cmd,
            sample_rate_hz,
            ..
        } => Ok(Box::new(PipeSource::new(
            cmd.clone(),
            *sample_rate_hz,
            source_id,
        )?)),
    }
}

/// Provider label for the bake-off report / provider→lead table / fanout legs.
fn source_label(s: &SourceConfig) -> String {
    s.label()
}

/// What the audio side of a run is made of. `Single` is the default path
/// (arbiter for ≥2 sources); `Fanout` (arbiter.mode = "fanout") carries one
/// full chain per source — own detector instance per leg — OR-ed at the shared
/// decision table by `run_pipeline_fanout`.
enum BuiltPipeline {
    Single {
        source: Box<dyn earshot::source::AudioSource>,
        detector: Box<dyn earshot::detector::Detector>,
    },
    Fanout {
        legs: Vec<earshot::pipeline::PipelineLeg>,
    },
}

/// Single construction point for the audio+detector side of `replay` and
/// `arm`, so replay validates the exact production path in both modes.
/// Alongside the graph it returns the bound Twilio listener port (first
/// Twilio leg; `None` when the config has no Twilio source) — the arm path's
/// `listener.ready` token reports it so the launcher dials the right door.
fn build_pipeline_input(
    cfg: &config::EventConfig,
    config_path: &Path,
    speed_override: Option<f32>,
    cancellation: Option<&SourceCancellation>,
) -> Result<(BuiltPipeline, Option<u16>), AnyErr> {
    let mut twilio_port = None;
    if cfg.arbiter.mode != config::ArbiterMode::Fanout {
        let source = build_source(
            cfg,
            config_path,
            speed_override,
            &mut twilio_port,
            cancellation,
        )?;
        let detector = detector_factory::build_detector(cfg)?;
        return Ok((BuiltPipeline::Single { source, detector }, twilio_port));
    }
    let twilio_auth = resolve_twilio_auth(cfg, config_path)?;
    let mut legs = Vec::with_capacity(cfg.sources.len());
    for (i, s) in cfg.sources.iter().enumerate() {
        let source = build_leg(
            s,
            config_path,
            speed_override,
            SourceId(i as u16),
            twilio_auth.as_ref(),
            None,
            &mut twilio_port,
        )?;
        legs.push(earshot::pipeline::PipelineLeg {
            label: source_label(s),
            source: with_source_cancellation(source, cancellation),
            // One detector instance per leg: model memory is deliberately
            // duplicated (docs/layers/detector.md).
            detector: detector_factory::build_detector(cfg)?,
        });
    }
    tracing::info!(
        legs = legs.len(),
        "fanout: per-leg detect chains, OR at decision"
    );
    Ok((BuiltPipeline::Fanout { legs }, twilio_port))
}

/// Arm-path readiness token consumed by `scripts/launch-event.sh` (contract:
/// `runtime.source.twilio.listener_ready` in `architecture/nodes.toml`).
const LISTENER_READY_FILE: &str = "listener.ready";
const STREAM_CONNECTED_FILE: &str = "stream.connected";
const DECISION_FAULT_FILE: &str = "decision.fault.json";
const ADMISSION_CHALLENGE_FILE: &str = "admission.challenge.json";
const ADMISSION_RECEIPT_FILE: &str = "admission.receipt.json";
const RUN_PLAN_ENV: &str = "EARSHOT_RUN_PLAN";
const EXECUTION_HOST_ENV: &str = "EARSHOT_EXECUTION_HOST";
const ACCOUNT_FINGERPRINT_ENV: &str = "EARSHOT_KALSHI_ACCOUNT_FINGERPRINT";
const BOOK_TAPE_CAPACITY: usize = 16_384;
const SOURCE_ATTACHED_FILE: &str = "source.attached";
const RUN_PLAN_SCHEMA: u64 = 3;
const RUN_PLAN_ARTIFACT_KEYS: [&str; 13] = [
    "receipts",
    "status",
    "audio_raw",
    "audio_frames",
    "audio_manifest",
    "audio_commit",
    "audio_lock",
    "audio_wav_dir",
    "book_tape",
    "order_evidence",
    "fix_observation_tape",
    "runtime_report",
    "grade",
];

#[derive(Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeRunPlan {
    schema: u64,
    kind: String,
    event_id: String,
    run_id: String,
    created_utc: String,
    starts_at_utc: String,
    lane: String,
    execution_enabled: bool,
    kalshi_env: String,
    market_event_ticker: String,
    execution_host_id: String,
    capture_only: bool,
    config_path: String,
    config_sha256: String,
    event_env_path: Option<String>,
    event_env_sha256: Option<String>,
    required_prep_receipts: Vec<String>,
    artifacts: std::collections::BTreeMap<String, String>,
    account_fingerprint: String,
    canonical_state_root: String,
    plan_sha256: String,
}

#[derive(Debug)]
struct VerifiedRunPlan {
    path: PathBuf,
    run_dir: PathBuf,
    plan: RuntimeRunPlan,
    audio_raw_path: PathBuf,
    audio_frames_path: PathBuf,
    audio_manifest_path: PathBuf,
    audio_commit_path: PathBuf,
    audio_lock_path: PathBuf,
    audio_wav_dir: PathBuf,
    book_tape_path: PathBuf,
    order_evidence_path: PathBuf,
    fix_observation_tape_path: PathBuf,
    runtime_report_path: PathBuf,
}

fn canonical_json_string(value: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for value in value.chars() {
        match value {
            '"' => out.extend_from_slice(br#"\""#),
            '\\' => out.extend_from_slice(br#"\\"#),
            '\u{08}' => out.extend_from_slice(br#"\b"#),
            '\u{0c}' => out.extend_from_slice(br#"\f"#),
            '\n' => out.extend_from_slice(br#"\n"#),
            '\r' => out.extend_from_slice(br#"\r"#),
            '\t' => out.extend_from_slice(br#"\t"#),
            value if (value as u32) < 0x20 || !value.is_ascii() => {
                let scalar = value as u32;
                if scalar <= 0xffff {
                    out.extend_from_slice(format!("\\u{scalar:04x}").as_bytes());
                } else {
                    let adjusted = scalar - 0x1_0000;
                    let high = 0xd800 + (adjusted >> 10);
                    let low = 0xdc00 + (adjusted & 0x3ff);
                    out.extend_from_slice(format!("\\u{high:04x}\\u{low:04x}").as_bytes());
                }
            }
            value => {
                let mut encoded = [0; 4];
                out.extend_from_slice(value.encode_utf8(&mut encoded).as_bytes());
            }
        }
    }
    out.push(b'"');
}

fn canonical_json(value: &serde_json::Value, out: &mut Vec<u8>) -> Result<(), AnyErr> {
    match value {
        serde_json::Value::Null => out.extend_from_slice(b"null"),
        serde_json::Value::Bool(value) => {
            out.extend_from_slice(if *value { b"true" } else { b"false" })
        }
        serde_json::Value::Number(value) => out.extend_from_slice(value.to_string().as_bytes()),
        serde_json::Value::String(value) => canonical_json_string(value, out),
        serde_json::Value::Array(values) => {
            out.push(b'[');
            for (index, value) in values.iter().enumerate() {
                if index != 0 {
                    out.push(b',');
                }
                canonical_json(value, out)?;
            }
            out.push(b']');
        }
        serde_json::Value::Object(values) => {
            out.push(b'{');
            let mut keys: Vec<_> = values.keys().collect();
            keys.sort();
            for (index, key) in keys.into_iter().enumerate() {
                if index != 0 {
                    out.push(b',');
                }
                canonical_json_string(key, out);
                out.push(b':');
                canonical_json(&values[key], out)?;
            }
            out.push(b'}');
        }
    }
    Ok(())
}

fn canonical_plan_sha256(mut value: serde_json::Value) -> Result<String, AnyErr> {
    use rsa::sha2::{Digest, Sha256};
    let object = value
        .as_object_mut()
        .ok_or("run plan must be a JSON object")?;
    object.remove("plan_sha256");
    let mut canonical = Vec::new();
    canonical_json(&value, &mut canonical)?;
    Ok(format!("{:x}", Sha256::digest(canonical)))
}

fn path_is_safe_relative(path: &str) -> bool {
    let path = Path::new(path);
    !path.as_os_str().is_empty()
        && !path.is_absolute()
        && path
            .components()
            .all(|part| matches!(part, std::path::Component::Normal(_)))
}

fn run_artifact_path(
    run_dir: &Path,
    artifacts: &std::collections::BTreeMap<String, String>,
    name: &str,
) -> Result<PathBuf, AnyErr> {
    let relative = artifacts
        .get(name)
        .ok_or_else(|| format!("run plan lacks required artifacts.{name}"))?;
    if !path_is_safe_relative(relative) {
        return Err(format!("run plan artifacts.{name} must be a safe run-relative path").into());
    }
    let candidate = run_dir.join(relative);
    let parent = candidate
        .parent()
        .ok_or_else(|| format!("run plan artifacts.{name} has no parent"))?;
    if !parent.is_dir() {
        return Err(format!(
            "RunPlan owner must pre-create {name} directory {}",
            parent.display()
        )
        .into());
    }
    let parent = parent.canonicalize()?;
    if !parent.starts_with(run_dir) {
        return Err(format!(
            "run plan artifacts.{name} parent escapes the canonical run directory"
        )
        .into());
    }
    Ok(parent.join(
        candidate
            .file_name()
            .ok_or_else(|| format!("run plan artifacts.{name} has no filename"))?,
    ))
}

fn verify_lifecycle_activation(plan: &RuntimeRunPlan, run_dir: &Path) -> Result<(), AnyErr> {
    use rsa::sha2::{Digest, Sha256};

    let receipts_path = run_artifact_path(run_dir, &plan.artifacts, "receipts")?;
    let metadata = std::fs::symlink_metadata(&receipts_path)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err("lifecycle receipts must be a regular non-symlink file".into());
    }
    let bytes = std::fs::read(&receipts_path)?;
    if bytes.is_empty() || !bytes.ends_with(b"\n") {
        return Err("lifecycle receipts are empty or have a torn final line".into());
    }
    let mut previous = plan.plan_sha256.clone();
    let mut next_sequence = 1u64;
    let mut reached = std::collections::HashSet::new();
    let lane_gate = if plan.lane == "phone" {
        "admission_resolved"
    } else {
        "access_acquired"
    };
    for (index, line) in bytes[..bytes.len() - 1]
        .split(|byte| *byte == b'\n')
        .enumerate()
    {
        if line.is_empty() {
            return Err(format!("lifecycle receipt line {} is blank", index + 1).into());
        }
        let value: serde_json::Value = serde_json::from_slice(line)?;
        let object = value
            .as_object()
            .ok_or_else(|| format!("lifecycle receipt line {} is not an object", index + 1))?;
        if value["schema"].as_u64() != Some(1)
            || value["event"].as_str() != Some(plan.event_id.as_str())
            || value["run"].as_str() != Some(plan.run_id.as_str())
            || value["sequence"].as_u64() != Some(next_sequence)
            || value["previous_sha256"].as_str() != Some(previous.as_str())
        {
            return Err(format!(
                "lifecycle receipt line {} has invalid identity/chain",
                index + 1
            )
            .into());
        }
        let claimed = value["receipt_sha256"]
            .as_str()
            .ok_or_else(|| format!("lifecycle receipt line {} lacks receipt_sha256", index + 1))?;
        let mut unhashed = value.clone();
        unhashed
            .as_object_mut()
            .expect("validated object")
            .remove("receipt_sha256");
        let mut canonical = Vec::new();
        canonical_json(&unhashed, &mut canonical)?;
        let actual = format!("{:x}", Sha256::digest(canonical));
        if claimed != actual {
            return Err(format!("lifecycle receipt line {} self-hash mismatch", index + 1).into());
        }
        let mut canonical_line = Vec::new();
        canonical_json(&value, &mut canonical_line)?;
        if canonical_line != line {
            return Err(format!("lifecycle receipt line {} is not canonical", index + 1).into());
        }
        if value["kind"].as_str() == Some("transition") && value["outcome"].as_str() == Some("ok") {
            if let Some(transition) = value["transition"].as_str() {
                if reached.contains(transition) {
                    return Err(format!(
                        "duplicate successful lifecycle transition {transition:?}"
                    )
                    .into());
                }
                let prerequisites: &[&str] = match transition {
                    "plan_compiled" => &[],
                    "prepared" | "study_completed" => &["plan_compiled"],
                    "admission_resolved" if plan.lane == "phone" => &["plan_compiled"],
                    "access_acquired" if plan.lane == "webcast" => &["plan_compiled"],
                    "preflight_passed" => &[],
                    "scheduled" | "capture_ready" => &["preflight_passed"],
                    "launched" => &["scheduled", "capture_ready"],
                    "source_attached" => &["launched"],
                    "supervising" => &["source_attached"],
                    "draining" => &["supervising"],
                    "completed" => &["draining"],
                    "evidence_pulled" | "graded" | "archived" | "descope" | "blocked"
                    | "aborted" | "failed" => &[],
                    other => {
                        return Err(
                            format!("unknown or wrong-lane lifecycle transition {other:?}").into(),
                        )
                    }
                };
                let missing: Vec<_> = if transition == "preflight_passed" {
                    plan.required_prep_receipts
                        .iter()
                        .filter(|required| !reached.contains(required.as_str()))
                        .map(String::as_str)
                        .collect()
                } else {
                    prerequisites
                        .iter()
                        .copied()
                        .filter(|required| !reached.contains(*required))
                        .collect()
                };
                if !missing.is_empty() {
                    return Err(format!(
                        "illegal lifecycle transition {transition:?}; missing {missing:?}"
                    )
                    .into());
                }
                reached.insert(transition.to_owned());
            }
        }
        previous = claimed.to_owned();
        next_sequence += 1;
        let _ = object;
    }
    if !reached.contains("preflight_passed") {
        return Err("lifecycle has no successful preflight_passed receipt".into());
    }
    for required in &plan.required_prep_receipts {
        if !reached.contains(required) {
            return Err(format!("lifecycle lacks required prep receipt {required:?}").into());
        }
    }
    if !reached.contains(lane_gate) {
        return Err(format!("lifecycle lacks lane gate {lane_gate:?}").into());
    }
    for required in ["scheduled", "capture_ready"] {
        if !reached.contains(required) {
            return Err(format!("lifecycle lacks pre-launch join receipt {required:?}").into());
        }
    }
    if reached.contains("launched") {
        return Err(
            "lifecycle already records launched; refusing a duplicate runtime activation".into(),
        );
    }
    if reached.iter().any(|transition| {
        matches!(
            transition.as_str(),
            "descope" | "blocked" | "aborted" | "failed" | "archived"
        )
    }) {
        return Err("lifecycle run is terminal or archived and cannot arm".into());
    }
    Ok(())
}

fn verify_runtime_run_plan(
    config_path: &Path,
    cfg: &config::EventConfig,
    kalshi_env: &KalshiEnv,
    canonical_state_root: &Path,
) -> Result<VerifiedRunPlan, AnyErr> {
    use rsa::sha2::{Digest, Sha256};
    let raw_path = std::env::var_os(RUN_PLAN_ENV).ok_or_else(|| {
        format!("live arm requires {RUN_PLAN_ENV}=<immutable control/run-plan.json>")
    })?;
    let path = PathBuf::from(raw_path).canonicalize()?;
    if path.file_name().and_then(|name| name.to_str()) != Some("run-plan.json")
        || path
            .parent()
            .and_then(Path::file_name)
            .and_then(|name| name.to_str())
            != Some("control")
    {
        return Err(format!(
            "{RUN_PLAN_ENV} must identify a control/run-plan.json, got {}",
            path.display()
        )
        .into());
    }
    let run_dir = path
        .parent()
        .and_then(Path::parent)
        .ok_or("run plan has no run directory")?
        .to_path_buf();
    let bytes = std::fs::read(&path)?;
    let value: serde_json::Value = serde_json::from_slice(&bytes)?;
    let actual_hash = canonical_plan_sha256(value.clone())?;
    let plan: RuntimeRunPlan = serde_json::from_value(value)?;
    if plan.schema != RUN_PLAN_SCHEMA || plan.kind != "run-plan" {
        return Err(format!("unsupported run plan schema/kind at {}", path.display()).into());
    }
    let actual_artifact_keys: std::collections::BTreeSet<_> =
        plan.artifacts.keys().map(String::as_str).collect();
    let expected_artifact_keys: std::collections::BTreeSet<_> =
        RUN_PLAN_ARTIFACT_KEYS.into_iter().collect();
    if actual_artifact_keys != expected_artifact_keys {
        return Err(format!(
            "run plan artifacts must exactly match schema {RUN_PLAN_SCHEMA}: expected {:?}, got {:?}",
            expected_artifact_keys, actual_artifact_keys
        )
        .into());
    }
    // Validate every schema destination, including control-owned artifacts
    // this process does not write.  Unsafe or aliased destinations make the
    // immutable plan internally ambiguous and must fail before any recorder
    // is opened.
    let mut resolved_artifacts = std::collections::BTreeSet::new();
    for name in RUN_PLAN_ARTIFACT_KEYS {
        let resolved = run_artifact_path(&run_dir, &plan.artifacts, name)?;
        if !resolved_artifacts.insert(resolved.clone()) {
            return Err(format!(
                "run plan artifacts must name distinct destinations; {name} aliases {}",
                resolved.display()
            )
            .into());
        }
    }
    if plan.plan_sha256 != actual_hash {
        return Err(format!("run plan self-hash mismatch at {}", path.display()).into());
    }
    if run_dir.file_name().and_then(|name| name.to_str()) != Some(plan.run_id.as_str())
        || run_dir
            .parent()
            .and_then(Path::file_name)
            .and_then(|name| name.to_str())
            != Some(plan.event_id.as_str())
    {
        return Err("run plan event/run identity does not match its directory".into());
    }
    if plan.event_id != cfg.event.id {
        return Err(format!(
            "run plan event {} != config event {}",
            plan.event_id, cfg.event.id
        )
        .into());
    }
    if !plan.execution_enabled {
        return Err("run plan explicitly disables execution".into());
    }
    let execution_host = std::env::var(EXECUTION_HOST_ENV)
        .map_err(|_| format!("live arm requires stable {EXECUTION_HOST_ENV}"))?;
    if execution_host != plan.execution_host_id {
        return Err(format!(
            "execution host {execution_host:?} != run plan host {:?}",
            plan.execution_host_id
        )
        .into());
    }
    let account_fingerprint = std::env::var(ACCOUNT_FINGERPRINT_ENV)
        .map_err(|_| format!("live arm requires stable {ACCOUNT_FINGERPRINT_ENV}"))?;
    if account_fingerprint != plan.account_fingerprint {
        return Err(
            "runtime Kalshi account fingerprint does not match the immutable run plan".into(),
        );
    }
    if plan.capture_only != cfg.keywords.is_empty() {
        return Err("run plan capture_only posture does not match the config".into());
    }
    if plan.kalshi_env != kalshi_env.name {
        return Err(format!(
            "run plan Kalshi environment {:?} != runtime {:?}",
            plan.kalshi_env, kalshi_env.name
        )
        .into());
    }
    let market_event = earshot::pipeline::canonical_market_event(&cfg.keywords)?;
    if plan.market_event_ticker != market_event {
        return Err(format!(
            "run plan market event {:?} != config {:?}",
            plan.market_event_ticker, market_event
        )
        .into());
    }
    let expected_lane = if config_has_twilio_source(cfg) {
        "phone"
    } else {
        "webcast"
    };
    if plan.lane != expected_lane {
        return Err(format!(
            "run plan lane {:?} != config lane {expected_lane:?}",
            plan.lane
        )
        .into());
    }
    if plan.starts_at_utc != cfg.event.starts_at {
        return Err("run plan start timestamp does not match the config".into());
    }
    let repo = std::env::var_os("EARSHOT_REPO")
        .map(PathBuf::from)
        .unwrap_or(std::env::current_dir()?)
        .canonicalize()?;
    if !path_is_safe_relative(&plan.canonical_state_root) {
        return Err("run plan canonical_state_root must be a safe repository-relative path".into());
    }
    let pinned_state_root = repo.join(&plan.canonical_state_root).canonicalize()?;
    if pinned_state_root != canonical_state_root {
        return Err(format!(
            "runtime canonical state root {} != run plan state root {}",
            canonical_state_root.display(),
            pinned_state_root.display()
        )
        .into());
    }
    let configured = config_path.canonicalize()?;
    let pinned_config = repo.join(&plan.config_path).canonicalize()?;
    if configured != pinned_config {
        return Err(format!(
            "runtime config {} != run plan config {}",
            configured.display(),
            pinned_config.display()
        )
        .into());
    }
    let config_hash = format!("{:x}", Sha256::digest(std::fs::read(&configured)?));
    if config_hash != plan.config_sha256 {
        return Err("runtime config SHA-256 does not match the immutable run plan".into());
    }
    match (&plan.event_env_path, &plan.event_env_sha256) {
        (Some(relative), Some(expected)) => {
            if !path_is_safe_relative(relative) {
                return Err(
                    "run plan event_env_path must be a safe repository-relative path".into(),
                );
            }
            let pinned = repo.join(relative).canonicalize()?;
            if !pinned.starts_with(&repo) {
                return Err("run plan event env escapes the repository".into());
            }
            let actual = format!("{:x}", Sha256::digest(std::fs::read(&pinned)?));
            if &actual != expected {
                return Err("event env SHA-256 does not match the immutable run plan".into());
            }
        }
        (None, None) => {}
        _ => return Err("run plan event env path/hash must appear together".into()),
    }
    if plan.required_prep_receipts.is_empty() {
        return Err("execution-enabled run plan must name required prep receipts".into());
    }
    for receipt in &plan.required_prep_receipts {
        if !path_is_safe_relative(receipt) {
            return Err(format!("unsafe required prep receipt {receipt:?}").into());
        }
    }
    let audio_raw_path = run_artifact_path(&run_dir, &plan.artifacts, "audio_raw")?;
    let audio_frames_path = run_artifact_path(&run_dir, &plan.artifacts, "audio_frames")?;
    let audio_manifest_path = run_artifact_path(&run_dir, &plan.artifacts, "audio_manifest")?;
    let audio_commit_path = run_artifact_path(&run_dir, &plan.artifacts, "audio_commit")?;
    let audio_lock_path = run_artifact_path(&run_dir, &plan.artifacts, "audio_lock")?;
    let audio_wav_dir = run_artifact_path(&run_dir, &plan.artifacts, "audio_wav_dir")?;
    let book_tape_path = run_artifact_path(&run_dir, &plan.artifacts, "book_tape")?;
    let order_evidence_path = run_artifact_path(&run_dir, &plan.artifacts, "order_evidence")?;
    let fix_observation_tape_path =
        run_artifact_path(&run_dir, &plan.artifacts, "fix_observation_tape")?;
    let runtime_report_path = run_artifact_path(&run_dir, &plan.artifacts, "runtime_report")?;
    verify_lifecycle_activation(&plan, &run_dir)?;
    let _ = &plan.created_utc;
    if kalshi_env.name != cfg.kalshi.env {
        return Err("resolved Kalshi environment drifted from config".into());
    }
    Ok(VerifiedRunPlan {
        path,
        run_dir,
        plan,
        audio_raw_path,
        audio_frames_path,
        audio_manifest_path,
        audio_commit_path,
        audio_lock_path,
        audio_wav_dir,
        book_tape_path,
        order_evidence_path,
        fix_observation_tape_path,
        runtime_report_path,
    })
}

fn config_has_phone_source(cfg: &config::EventConfig) -> bool {
    cfg.sources.iter().any(|source| {
        matches!(
            source,
            SourceConfig::TwilioStream { .. } | SourceConfig::Sip { .. }
        )
    })
}

fn config_has_twilio_source(cfg: &config::EventConfig) -> bool {
    cfg.sources
        .iter()
        .any(|source| matches!(source, SourceConfig::TwilioStream { .. }))
}

fn create_admission_interlock(
    cfg: &config::EventConfig,
    state_dir: &Path,
    live: bool,
) -> Result<DecisionAdmission, AnyErr> {
    if !live || !config_has_phone_source(cfg) {
        return Ok(DecisionAdmission::always_open());
    }
    use rsa::sha2::{Digest, Sha256};
    let wall_ns = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let seed = format!(
        "{}:{}:{wall_ns}:{}",
        cfg.event.id,
        std::process::id(),
        MonotonicNs::now().0
    );
    let nonce = format!("{:x}", Sha256::digest(seed.as_bytes()));
    let receipt_path = state_dir.join(ADMISSION_RECEIPT_FILE);
    match std::fs::remove_file(&receipt_path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(format!("remove stale {}: {error}", receipt_path.display()).into())
        }
    }
    let challenge_path = state_dir.join(ADMISSION_CHALLENGE_FILE);
    let stream_connected_path =
        config_has_twilio_source(cfg).then(|| state_dir.join(STREAM_CONNECTED_FILE));
    let challenge = serde_json::to_vec_pretty(&serde_json::json!({
        "schema_version": 1,
        "event_id": cfg.event.id,
        "nonce": nonce,
        "pid": std::process::id(),
        "receipt_path": receipt_path,
        "stream_connected_path": stream_connected_path,
    }))?;
    earshot::fsio::write_atomic(&challenge_path, &challenge)
        .map_err(|error| format!("write {}: {error}", challenge_path.display()))?;
    tracing::warn!(
        challenge = %challenge_path.display(),
        receipt = %receipt_path.display(),
        "phone decision admission CLOSED — detections are recorded but cannot consume one-shot state"
    );
    let event_id = cfg.event.id.clone();
    Ok(DecisionAdmission::new(
        move || {
            let Ok(bytes) = std::fs::read(&receipt_path) else {
                return false;
            };
            let Ok(receipt) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
                return false;
            };
            let base_matches = receipt["schema_version"].as_u64() == Some(1)
                && receipt["event_id"].as_str() == Some(event_id.as_str())
                && receipt["nonce"].as_str() == Some(nonce.as_str())
                && receipt["status"].as_str() == Some("admitted");
            if !base_matches {
                return false;
            }
            let Some(stream_connected_path) = stream_connected_path.as_ref() else {
                return true;
            };
            let Ok(marker_bytes) = std::fs::read(stream_connected_path) else {
                return false;
            };
            let Ok(marker) = serde_json::from_slice::<serde_json::Value>(&marker_bytes) else {
                return false;
            };
            marker["call_sid"].as_str() == receipt["call_sid"].as_str()
                && marker["attempt"].as_u64() == receipt["stream_attempt"].as_u64()
        },
        "pre_admission",
    ))
}

fn resolve_arm_paths(
    event_id: &str,
    state_dir: Option<PathBuf>,
    report_path: Option<PathBuf>,
) -> (PathBuf, PathBuf) {
    let state_dir = state_dir.unwrap_or_else(|| PathBuf::from("state").join(event_id));
    let report_path = report_path.unwrap_or_else(|| state_dir.join("report.json"));
    (state_dir, report_path)
}

/// Resolve and enforce the one state root that may arbitrate live execution
/// on this host. By default it is the `state/` directory beside the repository
/// marker that owns the selected config. A RunPlan may pin an absolute root
/// with `EARSHOT_CANONICAL_STATE_ROOT`; alternate per-invocation roots are
/// refused instead of silently acquiring independent one-shot locks.
fn enforce_live_state_root(
    config_path: &Path,
    state_dir: &Path,
    configured_root: Option<&Path>,
) -> Result<PathBuf, AnyErr> {
    let root = if let Some(root) = configured_root {
        if !root.is_absolute() {
            return Err(format!(
                "EARSHOT_CANONICAL_STATE_ROOT must be absolute, got {}",
                root.display()
            )
            .into());
        }
        root.to_path_buf()
    } else {
        let config = std::fs::canonicalize(config_path).map_err(|error| {
            format!(
                "canonicalize live config {}: {error}",
                config_path.display()
            )
        })?;
        let repo = config
            .ancestors()
            .find(|ancestor| ancestor.join("architecture/nodes.toml").is_file())
            .ok_or_else(|| {
                format!(
                    "live config {} is not inside an EARSHOT repository; set an absolute \
                     EARSHOT_CANONICAL_STATE_ROOT from the immutable RunPlan",
                    config.display()
                )
            })?;
        repo.join("state")
    };
    std::fs::create_dir_all(&root)
        .map_err(|error| format!("create canonical state root {}: {error}", root.display()))?;
    let root = std::fs::canonicalize(&root)
        .map_err(|error| format!("canonicalize state root {}: {error}", root.display()))?;
    let selected_parent = state_dir.parent().ok_or_else(|| {
        format!(
            "live state dir {} has no parent state root",
            state_dir.display()
        )
    })?;
    let selected_parent = std::fs::canonicalize(selected_parent).map_err(|error| {
        format!(
            "canonicalize selected state root {}: {error}; live state dirs must be \
             direct children of {}",
            selected_parent.display(),
            root.display()
        )
    })?;
    if selected_parent != root {
        return Err(format!(
            "live state dir {} is outside canonical root {}; alternate roots would bypass \
             the event decision lock",
            state_dir.display(),
            root.display()
        )
        .into());
    }
    Ok(root)
}

#[cfg(unix)]
fn process_is_alive(pid: u32) -> bool {
    if pid > i32::MAX as u32 {
        return false;
    }
    // SAFETY: signal 0 performs no mutation; it only checks whether the PID
    // exists and is signalable. EPERM still means the process exists.
    let rc = unsafe { libc::kill(pid as i32, 0) };
    rc == 0 || std::io::Error::last_os_error().kind() == std::io::ErrorKind::PermissionDenied
}

#[cfg(not(unix))]
fn process_is_alive(_pid: u32) -> bool {
    // The production targets are Unix. On an unsupported platform preserve
    // the token rather than deleting a possibly live arm's readiness door.
    true
}

/// Remove only a provably stale readiness token. Deleting first and then
/// discovering the canonical decision journal is locked would erase the live
/// first arm's dial/redial door. A token naming a live PID is therefore an
/// ownership conflict and is preserved; malformed/dead-owner tokens are
/// removed before any other startup refusal can return.
fn clear_stale_listener_ready(state_dir: &Path) -> Result<(), AnyErr> {
    let path = state_dir.join(LISTENER_READY_FILE);
    let raw = match std::fs::read(&path) {
        Ok(raw) => raw,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("read {}: {error}", path.display()).into()),
    };
    let owner = serde_json::from_slice::<serde_json::Value>(&raw)
        .ok()
        .and_then(|value| value.get("pid").and_then(serde_json::Value::as_u64))
        .and_then(|pid| u32::try_from(pid).ok());
    if let Some(pid) = owner {
        if process_is_alive(pid) {
            return Err(format!(
                "{} belongs to live pid {pid}; refusing a second arm without deleting its readiness door",
                path.display()
            )
            .into());
        }
    }
    std::fs::remove_file(&path).map_err(|error| -> AnyErr {
        format!("remove stale {}: {error}", path.display()).into()
    })?;
    tracing::warn!(path = %path.display(), ?owner, "removed stale listener readiness token");
    Ok(())
}

fn clear_stale_stream_connected(state_dir: &Path) -> Result<(), AnyErr> {
    let path = state_dir.join(STREAM_CONNECTED_FILE);
    match std::fs::remove_file(&path) {
        Ok(()) => {
            tracing::warn!(path = %path.display(), "removed stale stream-connected token");
            Ok(())
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("remove stale {}: {error}", path.display()).into()),
    }
}

fn clear_stale_source_attached(state_dir: &Path) -> Result<(), AnyErr> {
    let path = state_dir.join(SOURCE_ATTACHED_FILE);
    match std::fs::remove_file(&path) {
        Ok(()) => {
            tracing::warn!(path = %path.display(), "removed stale source-attached token");
            Ok(())
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("remove stale {}: {error}", path.display()).into()),
    }
}

async fn wait_for_decision_fault(health: DecisionHealth) -> String {
    loop {
        if let Some(detail) = health.fault_detail() {
            return detail;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

async fn wait_for_order_evidence_fault(
    health: earshot::kalshi::order_evidence::OrderEvidenceHealth,
) -> String {
    loop {
        if let Some(detail) = health.fault_detail() {
            return detail;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

async fn wait_for_audio_evidence_fault(
    bank: Option<&AudioEvidenceBank>,
    state_dir: &Path,
    run_plan: Option<&VerifiedRunPlan>,
) -> String {
    let Some(bank) = bank else {
        return std::future::pending::<String>().await;
    };
    let mut source_attached_written = false;
    let mut poll = tokio::time::interval(Duration::from_millis(25));
    poll.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        poll.tick().await;
        let health = bank.health();
        if !source_attached_written && health.accepted_frames > 0 {
            match persist_source_attached_marker(&health, state_dir, run_plan) {
                Ok(written) => source_attached_written = written,
                Err(error) => return error,
            }
        }
        if health.poisoned {
            return format_audio_evidence_health_fault(&health);
        }
    }
}

/// Publish the runtime `source_attached` door from the evidence bank's live
/// counters, never from socket/connect state.  The first frame accepted by the
/// same tap that feeds detection is the only fact that proves useful ingress.
/// Keeping this synchronous helper separate also closes the select race where
/// the pipeline and the health monitor become ready in one scheduler turn.
fn persist_source_attached_marker(
    health: &AudioEvidenceHealth,
    state_dir: &Path,
    run_plan: Option<&VerifiedRunPlan>,
) -> Result<bool, String> {
    if health.accepted_frames == 0 {
        return Ok(false);
    }
    let marker_path = state_dir.join(SOURCE_ATTACHED_FILE);
    if marker_path.exists() {
        return Ok(true);
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let marker = serde_json::json!({
        "schema_version": 1,
        "run_id": run_plan.map(|plan| plan.plan.run_id.as_str()),
        "event_id": run_plan.map_or("", |plan| plan.plan.event_id.as_str()),
        "plan_sha256": run_plan.map(|plan| plan.plan.plan_sha256.as_str()),
        "pid": std::process::id(),
        "unix_ns": now.as_secs().saturating_mul(1_000_000_000)
            + u64::from(now.subsec_nanos()),
        "accepted_frames": health.accepted_frames,
    });
    let bytes = serde_json::to_vec_pretty(&marker).map_err(|error| error.to_string())?;
    earshot::fsio::write_atomic(&marker_path, &bytes).map_err(|error| {
        format!(
            "source-attached marker could not be persisted at {}: {error}",
            marker_path.display()
        )
    })?;
    tracing::info!(
        path = %marker_path.display(),
        accepted_frames = health.accepted_frames,
        "detector-path audio attached"
    );
    Ok(true)
}

fn format_audio_evidence_health_fault(health: &AudioEvidenceHealth) -> String {
    format!(
        "audio evidence unhealthy: writer_error={:?} accepted={} written={} dropped={} closed_attempts={} invalid_attempts={} active_taps={} in_flight_taps={}",
        health.writer_error,
        health.accepted_frames,
        health.written_frames,
        health.dropped_frames,
        health.closed_attempts,
        health.invalid_attempts,
        health.active_taps,
        health.in_flight_taps,
    )
}

fn exact_json_string(value: &serde_json::Value, key: &str) -> Option<String> {
    value.get(key).and_then(|value| {
        value
            .as_str()
            .map(str::to_owned)
            .or_else(|| value.as_number().map(ToString::to_string))
    })
}

fn recovered_order_state(order: &serde_json::Value) -> (String, Option<bool>, bool) {
    let status = order["status"].as_str().unwrap_or("unknown").to_owned();
    let terminal = matches!(
        status.as_str(),
        "filled" | "canceled" | "rejected" | "expired"
    );
    let accepted = match status.as_str() {
        "rejected" => Some(false),
        "new" | "resting" | "pending" | "partial" | "filled" | "canceled" | "expired" => Some(true),
        _ => None,
    };
    (status, accepted, terminal)
}

fn recovered_fill(order: &serde_json::Value) -> Option<DurableFillEvidence> {
    let fill = DurableFillEvidence {
        filled_count_fp: exact_json_string(order, "fill_count"),
        remaining_count_fp: exact_json_string(order, "remaining_count"),
        average_price_dollars: exact_json_string(order, "average_fill_price"),
        latest_fill_count_fp: exact_json_string(order, "last_fill_count"),
        latest_fill_price_dollars: exact_json_string(order, "last_fill_price"),
        latest_fill_exchange_ts: order["last_fill_ts"].as_str().map(str::to_owned),
    };
    (fill.filled_count_fp.is_some()
        || fill.remaining_count_fp.is_some()
        || fill.average_price_dollars.is_some()
        || fill.latest_fill_count_fp.is_some()
        || fill.latest_fill_price_dollars.is_some()
        || fill.latest_fill_exchange_ts.is_some())
    .then_some(fill)
}

async fn reconcile_one_recovery(
    rest: &RestClient,
    sink: &OrderEvidenceSink,
    state: &earshot::kalshi::order_evidence::OrderRecoveryState,
) -> Result<bool, AnyErr> {
    let evidence = match rest
        .order_by_client_id_evidence(&state.client_order_id)
        .await
    {
        Ok(evidence) => evidence,
        Err(error) => {
            let observed_ts = MonotonicNs::now();
            sink.record_reconcile_unresolved(ReconcileUnresolved {
                client_order_id: state.client_order_id.clone(),
                market_ticker: state.market_ticker.clone(),
                protocol: TransportProtocol::Rest,
                endpoint_id: rest.endpoint_id(),
                observed_ts: observed_ts.0,
                queried_client_order_id: state.client_order_id.clone(),
                raw: None,
                parsed: None,
                detail: format!(
                    "startup exact-id reconciliation failed before response headers: {error}"
                ),
            })
            .await?;
            return Ok(false);
        }
    };
    let endpoint_id = rest.endpoint_id();
    let raw = RawEvidence::from_bytes("application/json", &evidence.raw_body);
    let parsed = evidence.parsed.clone();
    let Some(order) = evidence.matched_order else {
        let detail = evidence.unresolved_detail();
        sink.record_reconcile_unresolved(ReconcileUnresolved {
            client_order_id: state.client_order_id.clone(),
            market_ticker: state.market_ticker.clone(),
            protocol: TransportProtocol::Rest,
            endpoint_id,
            observed_ts: evidence.observed_ts.0,
            queried_client_order_id: evidence.queried_client_order_id,
            raw: Some(raw),
            parsed,
            detail,
        })
        .await?;
        return Ok(false);
    };
    let echoed = order["client_order_id"]
        .as_str()
        .ok_or("exact recovery row omitted client_order_id")?
        .to_owned();
    if echoed != state.client_order_id {
        return Err("exact recovery row echoed a foreign client_order_id".into());
    }
    let (status, accepted, terminal) = recovered_order_state(&order);
    let order_id = order["order_id"].as_str().map(str::to_owned);
    sink.record_reconciled(Reconciled {
        client_order_id: state.client_order_id.clone(),
        market_ticker: state.market_ticker.clone(),
        protocol: TransportProtocol::Rest,
        endpoint_id,
        observed_ts: evidence.observed_ts.0,
        queried_client_order_id: state.client_order_id.clone(),
        echoed_client_order_id: echoed,
        order_id,
        order_status: status,
        exchange_accepted: accepted,
        order_terminal: terminal,
        exchange_event_ts: order["updated_time"].as_str().map(str::to_owned),
        fill: recovered_fill(&order),
        raw,
        parsed,
    })
    .await?;
    Ok(terminal)
}

async fn recover_order_evidence_before_activation(
    rest: &RestClient,
    journal: &OrderEvidenceJournal,
) -> Result<(), AnyErr> {
    let sink = journal.sink();
    for state in journal.unresolved_recoveries() {
        match state.recovery_action() {
            Some(RecoveryAction::ConcludeNotAttempted) => {
                sink.record_not_attempted(NotAttempted {
                    client_order_id: state.client_order_id.clone(),
                    market_ticker: state.market_ticker.clone(),
                    refusal_ts: MonotonicNs::now().0,
                    reason: if state.send_request.is_some() {
                        "startup replay found no durable invocation-prepared boundary; transport was not invoked"
                    } else {
                        "startup replay found an unterminated evaluation with no send request; transport was not invoked"
                    }
                    .into(),
                })
                .await?;
                continue;
            }
            Some(RecoveryAction::ObserveOrCancelKnownOrder { order_id }) => {
                let _ = rest.cancel_orders(&[order_id]).await;
            }
            Some(RecoveryAction::ReconcileExactClientId) => {}
            None => continue,
        }
        if !reconcile_one_recovery(rest, &sink, &state).await? {
            return Err(format!(
                "order {} remains unresolved after exact-id recovery; live activation blocked",
                state.client_order_id
            )
            .into());
        }
    }
    Ok(())
}

/// Resolve every still-ambiguous session client id through the exact-id REST
/// door and attach any verified exchange id to the session registry. The
/// caller's next cancel sweep handles the id exactly once. `order_by_client_id`
/// refuses rows whose echoed client id is missing or different, so this can
/// never adopt an unrelated account order.
async fn reconcile_pending_clients(
    rest: &RestClient,
    registry: &earshot::kalshi::OrderRegistry,
) -> usize {
    let mut verified = 0;
    for client_id in registry.pending_client_ids() {
        match rest.order_by_client_id(&client_id).await {
            Ok(Some(order)) => match order["order_id"].as_str() {
                Some(order_id) => {
                    let order_id = order_id.to_string();
                    registry.register_for_client(&client_id, order_id.clone());
                    verified += 1;
                }
                None => tracing::error!(
                    %client_id,
                    "exact-id kill reconciliation returned a row without order_id; ambiguity remains"
                ),
            },
            Ok(None) => tracing::warn!(
                %client_id,
                "kill reconciliation found no exact-id row yet; absence is not proof"
            ),
            Err(error) => tracing::error!(
                %client_id,
                %error,
                "kill exact-id reconciliation failed; ambiguity remains"
            ),
        }
    }
    verified
}

/// Close the session send boundary, cancel every observed exchange id, and
/// continue exact-id reconciliation until all pre-kill transport calls have
/// returned. A bounded final retry window handles exchange-index propagation;
/// unresolved client ids remain explicit and are never blind-retried.
async fn kill_and_cancel_session(
    rest: &RestClient,
    registry: &earshot::kalshi::OrderRegistry,
) -> (usize, Vec<String>) {
    let (cancelled_ids, unresolved) = kill_and_cancel_session_with_timeout(
        rest,
        registry,
        Duration::from_secs(15),
        HashSet::new(),
    )
    .await;
    (cancelled_ids.len(), unresolved)
}

/// Bounded, resumable session cancellation. `cancelled_ids` is a proof set
/// from an earlier sweep; seeding it prevents an acknowledged DELETE from
/// being retried and then misclassified as unresolved during final drain.
async fn kill_and_cancel_session_with_timeout(
    rest: &RestClient,
    registry: &earshot::kalshi::OrderRegistry,
    timeout: Duration,
    mut cancelled_ids: HashSet<String>,
) -> (HashSet<String>, Vec<String>) {
    registry.kill();
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        let _ = reconcile_pending_clients(rest, registry).await;
        let outstanding_ids: Vec<String> = registry
            .ids()
            .into_iter()
            .filter(|id| !cancelled_ids.contains(id))
            .collect();
        if !outstanding_ids.is_empty() {
            for id in rest.cancel_orders_evidence(&outstanding_ids).await {
                cancelled_ids.insert(id);
            }
        }

        let pending_clients = registry.pending_client_ids();
        let unresolved_orders: Vec<String> = registry
            .ids()
            .into_iter()
            .filter(|id| !cancelled_ids.contains(id))
            .collect();
        if registry.active_attempts() == 0
            && pending_clients.is_empty()
            && unresolved_orders.is_empty()
        {
            break;
        }
        if tokio::time::Instant::now() >= deadline {
            break;
        }
        tokio::select! {
            _ = registry.changed() => {}
            _ = tokio::time::sleep(Duration::from_millis(100)) => {}
        }
    }
    let mut unresolved = registry.pending_client_ids();
    unresolved.extend(
        registry
            .ids()
            .into_iter()
            .filter(|id| !cancelled_ids.contains(id))
            .map(|id| format!("order_id:{id}")),
    );
    (cancelled_ids, unresolved)
}

async fn contain_decision_fault(
    state_dir: &Path,
    event_id: &str,
    journal_path: Option<&Path>,
    detail: &str,
    gate: &Arc<RiskGate>,
    rest: &RestClient,
    registry: &earshot::kalshi::OrderRegistry,
) -> HashSet<String> {
    gate.kill();
    registry.kill();
    for marker in [LISTENER_READY_FILE, STREAM_CONNECTED_FILE] {
        let path = state_dir.join(marker);
        if let Err(error) = std::fs::remove_file(&path) {
            if error.kind() != std::io::ErrorKind::NotFound {
                tracing::error!(path = %path.display(), %error, "failed to remove live marker after decision fault");
            }
        }
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let unix_ns = now.as_secs().saturating_mul(1_000_000_000) + u64::from(now.subsec_nanos());
    let receipt = serde_json::json!({
        "schema_version": 1,
        "unix_ns": unix_ns,
        "pid": std::process::id(),
        "event_id": event_id,
        "journal_path": journal_path.map(|path| path.display().to_string()),
        "action": "session_killed",
        "detail": detail,
    });
    let encoded =
        serde_json::to_vec_pretty(&receipt).expect("serializing a JSON Value cannot fail");
    let archive = state_dir.join(format!(
        "decision-fault-{unix_ns}-{}.json",
        std::process::id()
    ));
    if let Err(error) = earshot::fsio::write_atomic(&archive, &encoded) {
        tracing::error!(path = %archive.display(), %error, "FAILED to persist decision-fault archive");
    }
    let latest = state_dir.join(DECISION_FAULT_FILE);
    if let Err(error) = earshot::fsio::write_atomic(&latest, &encoded) {
        tracing::error!(path = %latest.display(), %error, "FAILED to persist latest decision-fault receipt");
    }
    let (cancelled_ids, unresolved_client_ids) = kill_and_cancel_session_with_timeout(
        rest,
        registry,
        Duration::from_secs(15),
        HashSet::new(),
    )
    .await;
    let cancelled = cancelled_ids.len();
    let ids = registry.ids();
    gate.flush();
    tracing::error!(
        %detail,
        cancelled,
        session_orders = ids.len(),
        unresolved_client_ids = unresolved_client_ids.len(),
        receipt = %archive.display(),
        "DECISION DURABILITY FAILED — session killed and orders cancelled"
    );
    cancelled_ids
}

async fn contain_book_tape_fault(
    state_dir: &Path,
    detail: &str,
    gate: &Arc<RiskGate>,
    rest: &RestClient,
    registry: &earshot::kalshi::OrderRegistry,
) -> HashSet<String> {
    contain_book_tape_fault_with_timeout(
        state_dir,
        detail,
        gate,
        rest,
        registry,
        Duration::from_secs(15),
    )
    .await
}

async fn contain_book_tape_fault_with_timeout(
    state_dir: &Path,
    detail: &str,
    gate: &Arc<RiskGate>,
    rest: &RestClient,
    registry: &earshot::kalshi::OrderRegistry,
    timeout: Duration,
) -> HashSet<String> {
    gate.kill();
    registry.kill();
    for marker in [LISTENER_READY_FILE, STREAM_CONNECTED_FILE] {
        let path = state_dir.join(marker);
        if let Err(error) = std::fs::remove_file(&path) {
            if error.kind() != std::io::ErrorKind::NotFound {
                tracing::error!(path = %path.display(), %error, "failed to remove live marker after book evidence fault");
            }
        }
    }
    let (cancelled_ids, unresolved_client_ids) =
        kill_and_cancel_session_with_timeout(rest, registry, timeout, HashSet::new()).await;
    let cancelled = cancelled_ids.len();
    let ids = registry.ids();
    gate.flush();
    tracing::error!(
        %detail,
        cancelled,
        session_orders = ids.len(),
        unresolved_client_ids = unresolved_client_ids.len(),
        "BOOK TAPE EVIDENCE FAILED — session killed and orders cancelled"
    );
    cancelled_ids
}

/// Keep sweeping only newly observed session order ids while a poisoned
/// decision graph drains.  An order may have crossed the transport boundary
/// just before the gate was killed and register its exchange id just after
/// the first cancellation snapshot; waiting for source EOF before looking
/// again would leave that order live for the rest of a call.
async fn await_pipeline_with_fault_cancellation<T>(
    pipeline_task: &mut tokio::task::JoinHandle<T>,
    rest: &RestClient,
    registry: &earshot::kalshi::OrderRegistry,
    cancellation_attempted: HashSet<String>,
) -> (Result<T, tokio::task::JoinError>, HashSet<String>) {
    let mut cancellation_confirmed = cancellation_attempted;
    let mut sweep = tokio::time::interval(Duration::from_millis(50));
    sweep.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            outcome = &mut *pipeline_task => return (outcome, cancellation_confirmed),
            _ = sweep.tick() => {
                let newly_seen: Vec<String> = registry
                    .ids()
                    .into_iter()
                    .filter(|id| !cancellation_confirmed.contains(id))
                    .collect();
                if !newly_seen.is_empty() {
                    let cancelled = rest.cancel_orders_evidence(&newly_seen).await;
                    let cancelled_count = cancelled.len();
                    cancellation_confirmed.extend(cancelled);
                    tracing::error!(
                        cancelled = cancelled_count,
                        newly_registered = newly_seen.len(),
                        "decision-fault drain: cancelled late-registered session orders"
                    );
                }
                if !registry.pending_client_ids().is_empty() {
                    let verified = reconcile_pending_clients(rest, registry).await;
                    if verified > 0 {
                        tracing::error!(verified, "decision-fault drain: reconciled pending client ids for next cancellation sweep");
                    }
                }
            }
        }
    }
}

/// Atomically write `<state_dir>/listener.ready` —
/// `{"port":<u16>,"pid":<u32>,"unix_ts":<int-seconds>}`. `port` is the bound
/// Twilio listener port, or 0 for configs without a Twilio source. Fatal on
/// failure: a run whose token can't be written would never be dialed, the
/// same silent-death class the token exists to prevent.
fn write_listener_ready(state_dir: &Path, port: u16) -> Result<(), AnyErr> {
    let unix_ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let body = format!(
        "{{\"port\":{port},\"pid\":{},\"unix_ts\":{unix_ts}}}\n",
        std::process::id()
    );
    let path = state_dir.join(LISTENER_READY_FILE);
    earshot::fsio::write_atomic(&path, body.as_bytes())
        .map_err(|e| format!("write {}: {e}", path.display()))?;
    tracing::info!(path = %path.display(), port, "listener.ready written (detector built, pipeline spawned) — dial may proceed");
    Ok(())
}

/// Shared bring-up tail: wrap a built signer in a REST client and run the signed
/// `balance()` self-test + clock-skew guard. The self-test is also the proof that
/// the (key_id, private key) pair is a valid, coherent Kalshi credential — a
/// mismatched key file fails the signature here and refuses to arm.
async fn finish_connect(
    env: &KalshiEnv,
    signer: Signer,
) -> Result<(Arc<Signer>, RestClient), AnyErr> {
    let signer = Arc::new(signer);
    let rest = RestClient::new(env, signer.clone())?;
    rest.balance()
        .await
        .map_err(|e| format!("signed balance self-test failed: {e}"))?;
    let offset = rest.clock_offset_estimate().await?;
    tracing::info!(
        offset_secs = format!("{offset:+.2}"),
        "clock offset vs Kalshi"
    );
    if offset.abs() > 10.0 {
        return Err(format!(
            "clock skew {offset:+.1}s vs Kalshi exceeds 10 s guard (window is ±30 s) — fix NTP first"
        )
        .into());
    }
    Ok((signer, rest))
}

/// Env-credential bring-up for the single-account operational commands
/// (cancel/orders/probe/kill-drill). Reads `KALSHI_API_KEY_ID` /
/// `KALSHI_PRIVATE_KEY_PATH` from the process environment.
async fn connect_live() -> Result<(Arc<Signer>, RestClient), AnyErr> {
    finish_connect(&KalshiEnv::prod(), Signer::from_env()?).await
}

/// Select the exact credential `arm` will use without performing any network
/// operation.  Keeping this branch shared with the fingerprint CLI prevents a
/// provisioning helper from describing a different account than the runtime.
fn load_signer_for(cfg: &config::EventConfig, config_path: &Path) -> Result<Signer, AnyErr> {
    let k = &cfg.kalshi;
    let signer = match (&k.key_id, &k.private_key_path) {
        (Some(key_id), Some(pk_path)) => {
            let pem_path = resolve_relative(config_path, pk_path);
            let pem = std::fs::read_to_string(&pem_path).map_err(|e| {
                format!("read [kalshi].private_key_path {}: {e}", pem_path.display())
            })?;
            Signer::from_pem(key_id.clone(), &pem)?
        }
        _ => Signer::from_env()?,
    };
    Ok(signer)
}

/// Per-event bring-up for `arm`: bind the Kalshi credential to THIS config when
/// `[kalshi] key_id` + `private_key_path` are set (multi-event isolation — the
/// signer physically cannot be another event's key), else fall back to env.
/// Config-internal consistency (`expected_key_id`) is already enforced by
/// `config::validate`; the remote coherence proof is the `balance()` self-test.
async fn connect_live_for(
    cfg: &config::EventConfig,
    config_path: &Path,
    env: &KalshiEnv,
) -> Result<(Arc<Signer>, RestClient), AnyErr> {
    let config_bound = cfg.kalshi.key_id.as_deref();
    let signer = load_signer_for(cfg, config_path)?;
    if let Some(key_id) = config_bound {
        tracing::info!(
            key_id,
            "kalshi credential bound from config (per-event account)"
        );
    } else {
        tracing::info!("kalshi credential from env (single-account posture)");
    }
    finish_connect(env, signer).await
}

/// Resolve `p` relative to the directory holding `config_path` (absolute paths
/// pass through). Mirrors the WAV-path logic in `build_leg`.
fn resolve_relative(config_path: &Path, p: &str) -> PathBuf {
    let p = PathBuf::from(p);
    if p.is_absolute() {
        p
    } else {
        config_path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(p)
    }
}

async fn wait_for_books(
    cache: &BookCache,
    tickers: &[String],
    timeout: Duration,
) -> Result<(), AnyErr> {
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

async fn wait_for_book_tape_failure(
    failures: &mut Option<tokio::sync::watch::Receiver<Option<String>>>,
) -> String {
    match failures {
        Some(failures) => match failures.changed().await {
            Ok(()) => failures
                .borrow()
                .clone()
                .unwrap_or_else(|| "book tape failure notification had no detail".to_string()),
            Err(_) => "book tape failure channel closed unexpectedly".to_string(),
        },
        None => std::future::pending::<String>().await,
    }
}

async fn wait_for_book_tape_completion(
    tape: &mut Option<BookTapeHandle>,
) -> Result<BookTapeSummary, String> {
    match tape {
        Some(tape) => tape.wait().await,
        None => std::future::pending::<Result<BookTapeSummary, String>>().await,
    }
}

fn premature_book_tape_completion(result: &Result<BookTapeSummary, String>) -> String {
    match result {
        Ok(summary) => format!(
            "book tape writer stopped before runtime drain: status={:?} complete={} committed={} reason={}",
            summary.status, summary.complete, summary.committed, summary.reason
        ),
        Err(error) => format!("book tape writer task failed before runtime drain: {error}"),
    }
}

/// Close intent precedes producer shutdown. Awaiting the aborted WS task proves
/// its `FrameTap` was dropped before the tape writer is joined and judged.
async fn finalize_book_feed(
    mut ws: tokio::task::JoinHandle<()>,
    mut tape: Option<BookTapeHandle>,
    request_close: bool,
) -> (Option<BookTapeSummary>, Option<String>) {
    let mut failures = Vec::new();
    if request_close {
        if let Some(tape) = tape.as_mut() {
            if let Err(error) = tape.request_close(BookTapeCloseReason::Requested) {
                failures.push(format!("book tape close request failed: {error}"));
            }
        }
    }

    ws.abort();
    if let Err(error) = (&mut ws).await {
        if !error.is_cancelled() {
            failures.push(format!(
                "book WebSocket task failed during shutdown: {error}"
            ));
        }
    }

    let summary = if let Some(tape) = tape.as_mut() {
        match tokio::time::timeout(Duration::from_secs(12), tape.wait()).await {
            Ok(Ok(summary)) => {
                if !summary.complete {
                    failures.push(format!(
                        "book tape incomplete: status={:?} committed={} dropped={} unwritten={} reason={}",
                        summary.status,
                        summary.committed,
                        summary.tap_dropped,
                        summary.unwritten_frames,
                        summary.reason
                    ));
                }
                if let Some(error) = summary.writer_error.as_deref() {
                    failures.push(format!("book tape writer failed: {error}"));
                }
                Some(summary)
            }
            Ok(Err(error)) => {
                failures.push(format!("book tape join failed: {error}"));
                None
            }
            Err(_) => {
                failures.push("book tape shutdown exceeded 12 seconds".to_string());
                None
            }
        }
    } else {
        None
    };

    let detail = (!failures.is_empty()).then(|| failures.join("; "));
    (summary, detail)
}

struct RuntimePipelineResult {
    result: Result<PipelineReport, PipelineFailure>,
    audio_evidence: Option<AudioEvidenceForwardReport>,
    audio_evidence_error: Option<String>,
}

impl RuntimePipelineResult {
    fn plain(result: Result<PipelineReport, PipelineFailure>) -> Self {
        Self {
            result,
            audio_evidence: None,
            audio_evidence_error: None,
        }
    }

    fn audio(result: Result<PipelineWithAudioReport, PipelineWithAudioFailure>) -> Self {
        match result {
            Ok(result) => Self {
                result: Ok(result.pipeline),
                audio_evidence: Some(result.audio_evidence),
                audio_evidence_error: None,
            },
            Err(PipelineWithAudioFailure::Pipeline {
                failure,
                audio_evidence,
            }) => Self {
                result: Err(failure),
                audio_evidence: Some(audio_evidence),
                audio_evidence_error: None,
            },
            Err(PipelineWithAudioFailure::AudioEvidence {
                partial,
                audio_evidence,
                concurrent_pipeline_failure,
            }) => {
                let detail = audio_evidence
                    .first_error()
                    .map(ToString::to_string)
                    .unwrap_or_else(|| {
                        "audio evidence forwarding accounting is inconsistent".to_string()
                    });
                let failure = match concurrent_pipeline_failure {
                    Some(mut failure) => {
                        failure.partial = Some(partial);
                        *failure
                    }
                    None => PipelineFailure {
                        error: SourceError::Protocol(detail.clone()),
                        partial: Some(partial),
                        kind: PipelineFailureKind::Source,
                    },
                };
                Self {
                    result: Err(failure),
                    audio_evidence: Some(audio_evidence),
                    audio_evidence_error: Some(detail),
                }
            }
        }
    }

    fn published_audio_frames(&self) -> Option<u64> {
        self.audio_evidence
            .as_ref()
            .map(|evidence| evidence.published_frames)
    }
}

#[derive(Debug)]
enum AudioEvidenceTerminal {
    Committed(AudioEvidenceCommit),
    Partial(AudioEvidencePartial),
    Failed(String),
    NotApplicable,
}

async fn finalize_audio_evidence(
    bank: Option<AudioEvidenceBank>,
    expected_published_frames: Option<u64>,
    clean: bool,
    reason: String,
) -> AudioEvidenceTerminal {
    let Some(bank) = bank else {
        return AudioEvidenceTerminal::NotApplicable;
    };
    let finish = clean && expected_published_frames.is_some_and(|frames| frames > 0);
    match tokio::task::spawn_blocking(move || {
        if finish {
            bank.finish(expected_published_frames.expect("checked above"))
                .map(AudioEvidenceTerminal::Committed)
                .map_err(|error| error.to_string())
        } else {
            bank.abort(&reason)
                .map(AudioEvidenceTerminal::Partial)
                .map_err(|error| error.to_string())
        }
    })
    .await
    {
        Ok(Ok(terminal)) => terminal,
        Ok(Err(error)) => AudioEvidenceTerminal::Failed(error),
        Err(error) => {
            AudioEvidenceTerminal::Failed(format!("audio evidence finalizer task failed: {error}"))
        }
    }
}

async fn wait_for_fix_observation_failure(
    failures: &mut Option<tokio::sync::watch::Receiver<Option<String>>>,
) -> String {
    match failures {
        Some(failures) => match failures.changed().await {
            Ok(()) => failures
                .borrow()
                .clone()
                .unwrap_or_else(|| "FIX observation failure had no detail".to_string()),
            Err(_) => "FIX observation failure channel closed unexpectedly".to_string(),
        },
        None => std::future::pending::<String>().await,
    }
}

async fn wait_for_fix_observation_completion(
    tape: &mut Option<FixObservationTapeHandle>,
) -> Result<FixObservationTapeSummary, String> {
    match tape {
        Some(tape) => tape.wait().await,
        None => std::future::pending::<Result<FixObservationTapeSummary, String>>().await,
    }
}

async fn finalize_fix_observation_tape(
    mut tape: Option<FixObservationTapeHandle>,
    fix: Option<Arc<earshot::kalshi::fix::transport::FixTransport>>,
    reason: FixObservationTapeCloseReason,
) -> (Option<FixObservationTapeSummary>, Option<String>) {
    let mut failures = Vec::new();
    if let Some(tape) = tape.as_mut() {
        if let Err(error) = tape.request_close(reason) {
            failures.push(format!("FIX observation close request failed: {error}"));
        }
    }
    if let Some(fix) = fix {
        if tokio::time::timeout(Duration::from_secs(4), fix.shutdown())
            .await
            .is_err()
        {
            failures.push("FIX Logout/shutdown exceeded 4 seconds".to_string());
        }
        drop(fix);
    }
    let Some(mut tape) = tape.take() else {
        return (None, (!failures.is_empty()).then(|| failures.join("; ")));
    };
    match tokio::time::timeout(Duration::from_secs(7), tape.wait()).await {
        Ok(Ok(summary)) => {
            if !summary.complete || !summary.committed {
                failures.push(format!(
                    "FIX observation tape incomplete: complete={} committed={} dropped={} unwritten={} reason={}",
                    summary.complete,
                    summary.committed,
                    summary.dropped_observations,
                    summary.unwritten_observations,
                    summary.reason,
                ));
            }
            if let Some(error) = summary.writer_error.as_deref() {
                failures.push(format!("FIX observation writer failed: {error}"));
            }
            let detail = (!failures.is_empty()).then(|| failures.join("; "));
            (Some(summary), detail)
        }
        Ok(Err(error)) => {
            failures.push(format!("FIX observation join failed: {error}"));
            (None, Some(failures.join("; ")))
        }
        Err(_) => {
            failures.push("FIX observation shutdown exceeded 7 seconds".to_string());
            (None, Some(failures.join("; ")))
        }
    }
}

fn spawn_owned_presigner_refresher(presigner: Arc<Presigner>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(PRESIGN_REFRESH);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tick.tick().await;
            presigner.refresh();
        }
    })
}

async fn arm(
    config_path: PathBuf,
    report_path: Option<PathBuf>,
    i_am_live: bool,
    speed: Option<f32>,
    allow_mention: bool,
    fillable: bool,
    state_dir: Option<PathBuf>,
) -> Result<(), AnyErr> {
    if fillable && !allow_mention {
        return Err("--fillable requires --allow-mention (it is the mention-FILL posture)".into());
    }
    if fillable && !i_am_live {
        return Err(
            "--fillable requires --i-am-live (dry-run never sends, fillable is meaningless)".into(),
        );
    }
    let cfg = config::load(&config_path)?;
    let (state_dir, mut report_path) = resolve_arm_paths(&cfg.event.id, state_dir, report_path);
    let kalshi_env = KalshiEnv::from_name(&cfg.kalshi.env).ok_or_else(|| {
        format!(
            "unsupported [kalshi].env {:?}; expected prod or demo",
            cfg.kalshi.env
        )
    })?;
    if cfg.kalshi.transport == config::OrderWire::Fix {
        if kalshi_env.name != "prod" {
            return Err(
                "FIX transport is only wired for prod; refusing an env/endpoint mismatch".into(),
            );
        }
        let fix = cfg.kalshi.fix.as_ref();
        let host = fix
            .map(|fix| fix.host.as_str())
            .unwrap_or("mm.fix.elections.kalshi.com");
        let port = fix.map(|fix| fix.port).unwrap_or(8228);
        if i_am_live && (host != "mm.fix.elections.kalshi.com" || port != 8228) {
            return Err(format!(
                "live FIX endpoint {host}:{port} is not the verified prod endpoint \
                 mm.fix.elections.kalshi.com:8228; refusing to key prod decision state \
                 against an unverified destination"
            )
            .into());
        }
    }
    config::allowlist_covers_keywords(&cfg).map_err(|e| -> AnyErr { e.into() })?;
    let redial_dir = std::env::var("EARSHOT_REDIAL_DIR").ok();
    config::check_redial_dir_for_persistence(&cfg, redial_dir.as_deref())
        .map_err(|e| -> AnyErr { e.into() })?;
    if !i_am_live && cfg.risk.allowlist_tickers.is_empty() {
        tracing::warn!(
            "dry-run with an EMPTY [risk].allowlist_tickers — every order would be \
             refused NotAllowlisted if this config were armed live. A clean dry-run \
             here proves the audio path, NOT the fire path."
        );
    }
    let state_dir = state_dir.as_path();
    std::fs::create_dir_all(state_dir)?;
    let canonical_state_root = if i_am_live {
        let configured_root = std::env::var_os("EARSHOT_CANONICAL_STATE_ROOT").map(PathBuf::from);
        let root = enforce_live_state_root(&config_path, state_dir, configured_root.as_deref())?;
        tracing::info!(root = %root.display(), "canonical live state root enforced");
        Some(root)
    } else {
        None
    };
    // Clear only dead-owner readiness before another startup check can fail.
    // A live owner's token is preserved and makes this arm refuse.
    clear_stale_listener_ready(state_dir)?;
    // Phone/webcast source variants have different [event].id/state dirs but
    // can trade the same market event. The assembly boundary derives one
    // source-neutral env+market-event journal beside those lane directories;
    // a second variant therefore collides on the same lock and cannot fire.
    // Dry runs remain ephemeral and never create or mutate this state.
    let prepared_decisions =
        prepare_decision_state(&cfg.keywords, kalshi_env.name, state_dir, i_am_live)?;
    let verified_run_plan = if i_am_live {
        Some(verify_runtime_run_plan(
            &config_path,
            &cfg,
            &kalshi_env,
            canonical_state_root
                .as_deref()
                .expect("live state root was enforced"),
        )?)
    } else {
        None
    };
    // Ownership is established by the decision lease and immutable RunPlan
    // verification above. Only now may this attempt remove the prior
    // detector-path attachment receipt.
    if i_am_live {
        clear_stale_source_attached(state_dir)?;
    }
    if let Some(run_plan) = verified_run_plan.as_ref() {
        let selected = if report_path.exists() {
            report_path.canonicalize()?
        } else {
            let parent = report_path
                .parent()
                .ok_or("runtime report path has no parent")?
                .canonicalize()?;
            parent.join(
                report_path
                    .file_name()
                    .ok_or("runtime report path has no filename")?,
            )
        };
        if selected != run_plan.runtime_report_path {
            return Err(format!(
                "runtime report {} is not immutable RunPlan artifact {}",
                selected.display(),
                run_plan.runtime_report_path.display()
            )
            .into());
        }
        report_path = run_plan.runtime_report_path.clone();
    }
    let decision_table = prepared_decisions.table;
    let decision_health = prepared_decisions.health;
    let decision_lease = prepared_decisions.lease;
    let decision_journal_path = prepared_decisions.journal_path;
    let decision_plan_sha1 = prepared_decisions.decision_plan_sha1;
    // Phone media begins with IVR/hold/operator audio. Build a fail-closed
    // admission challenge before source construction; the supervisor must
    // echo this exact run nonce before detections may reach DecisionTable.
    let decision_admission = create_admission_interlock(&cfg, state_dir, i_am_live)?;
    // Live ownership is authoritative only after the journal lease exists.
    // Removing this marker earlier lets a racing second arm erase the first
    // arm's connected-stream evidence before losing the journal lock.
    clear_stale_stream_connected(state_dir)?;
    if let Some(path) = &decision_journal_path {
        tracing::info!(path = %path.display(), "canonical decision state locked and replayed");
    }
    let (signer, rest) = connect_live_for(&cfg, &config_path, &kalshi_env).await?;
    if let Some(run_plan) = verified_run_plan.as_ref() {
        let actual_fingerprint = signer.account_fingerprint();
        if actual_fingerprint != run_plan.plan.account_fingerprint {
            return Err(format!(
                "actual Kalshi signer fingerprint {actual_fingerprint} does not match immutable RunPlan"
            )
            .into());
        }
    }

    // Arm-time market validation (rules stored for audit; refuse on any miss).
    let validator = LiveMarketValidator {
        rest: &rest,
        state_dir,
    };
    let markets = validator.validate(&cfg).await?;
    tracing::info!(
        markets = markets.len(),
        "all keyword markets active; rules stored to state/rules/"
    );

    let gate = Arc::new(RiskGate::new(cfg.risk.clone(), state_dir)?);
    let live_max_cap = if i_am_live {
        let max_cap = cfg
            .keywords
            .iter()
            .map(|k| k.max_price_cents)
            .max()
            .unwrap_or(0);
        if fillable {
            mention_fill_gate(&markets, max_cap)?;
        } else if allow_mention {
            mention_probe_gate(&markets, max_cap)?;
        } else {
            m2_live_gate(&markets, max_cap)?;
        }
        if gate.cfg().allowlist_tickers.is_empty() {
            return Err("--i-am-live requires a non-empty [risk].allowlist_tickers".into());
        }
        Some(max_cap)
    } else {
        tracing::info!("dry-run: orders will be computed and journaled, never sent");
        None
    };

    // The cache key-set is immutable. Multiple phrases may intentionally map
    // to one market, but the WS subscription and evidence session name each
    // ticker exactly once.
    let mut tickers: Vec<String> = cfg
        .keywords
        .iter()
        .map(|k| k.market_ticker.clone())
        .collect();
    tickers.sort();
    tickers.dedup();
    let cache = BookCache::new(&tickers);

    // Construct the complete audio graph before opening the FIX receive tape
    // or either market-data recorder. Twilio may bind here, but
    // listener.ready is still absent, so phone-session control cannot dial
    // into a half-constructed runtime.
    let source_cancellation = SourceCancellation::default();
    let (built, twilio_port) =
        build_pipeline_input(&cfg, &config_path, speed, Some(&source_cancellation))?;

    // The order journal is the first run-scoped recorder opened. Every
    // fallible validation and graph construction above therefore refuses
    // without creating a partial attempt. Once open, all later startup exits
    // explicitly close it (and any recorder opened after it).
    let mut order_evidence_journal = if let Some(run_plan) = verified_run_plan.as_ref() {
        let journal = OrderEvidenceJournal::open(
            &run_plan.order_evidence_path,
            OrderEvidenceIdentity {
                run_id: run_plan.plan.run_id.clone(),
                event_id: run_plan.plan.event_id.clone(),
                kalshi_env: run_plan.plan.kalshi_env.clone(),
                account_fingerprint: run_plan.plan.account_fingerprint.clone(),
                plan_sha256: run_plan.plan.plan_sha256.clone(),
            },
        )?;
        if let Err(error) = recover_order_evidence_before_activation(&rest, &journal).await {
            let close = journal.close().err().map(|error| error.to_string());
            return Err(format!(
                "order evidence recovery refused activation: {error}; close_error={close:?}"
            )
            .into());
        }
        tracing::info!(
            plan = %run_plan.path.display(),
            run_dir = %run_plan.run_dir.display(),
            journal = %run_plan.order_evidence_path.display(),
            "immutable RunPlan verified and order evidence locked"
        );
        Some(journal)
    } else {
        None
    };
    let order_evidence_sink = order_evidence_journal
        .as_ref()
        .map(OrderEvidenceJournal::sink);
    let order_evidence_health = order_evidence_journal
        .as_ref()
        .map(OrderEvidenceJournal::health);

    macro_rules! close_order_evidence_before_activation {
        () => {{
            order_evidence_journal
                .take()
                .and_then(|journal| journal.close().err())
                .map(|error| error.to_string())
        }};
    }

    // Order-entry wire (config `[kalshi] transport`, default rest). The FIX
    // arm connects + logs on NOW — fail-fast before the pipeline starts, the
    // same posture as `wait_for_books`.
    let prestaged_wire = match cfg.kalshi.transport {
        config::OrderWire::Rest => earshot::kalshi::transport::StageWire::Rest,
        config::OrderWire::Fix => {
            // The actual config reference is supplied after constructing the
            // FIX transport; this value is not used for FIX prestaging.
            earshot::kalshi::transport::StageWire::Rest
        }
    };
    let order_run_identity = verified_run_plan
        .as_ref()
        .map(|plan| plan.plan.plan_sha256.as_str())
        .unwrap_or(cfg.event.id.as_str());
    let rest_prestaged = (cfg.kalshi.transport == config::OrderWire::Rest)
        .then(|| prestage_orders(order_run_identity, &cfg.keywords, prestaged_wire));
    let mut fix_observation_tape: Option<FixObservationTapeHandle> = None;
    let mut fix_observation_failures: Option<tokio::sync::watch::Receiver<Option<String>>> = None;
    let mut fix_observation_health: Option<FixObservationTapeHealth> = None;
    let mut fix_transport_owner: Option<Arc<earshot::kalshi::fix::transport::FixTransport>> = None;
    let mut rest_presigner_for_refresher: Option<Arc<Presigner>> = None;
    let (transport, prestaged) = match cfg.kalshi.transport {
        config::OrderWire::Rest => {
            // Presigned order headers, refreshed every 10 s (measured ±30 s window).
            let presigner = Presigner::new(
                signer.clone(),
                "POST",
                format!("/trade-api/v2{ORDERS_ENDPOINT}"),
            );
            rest_presigner_for_refresher = Some(presigner.clone());
            (
                OrderTransport::Rest {
                    client: rest.clone(),
                    presigner,
                },
                rest_prestaged.expect("REST prestaging prepared"),
            )
        }
        config::OrderWire::Fix => {
            let f = cfg.kalshi.fix.as_ref();
            let fix_cfg = earshot::kalshi::fix::FixConfig {
                host: f
                    .map(|f| f.host.clone())
                    .unwrap_or_else(|| "mm.fix.elections.kalshi.com".into()),
                port: f.map(|f| f.port).unwrap_or(8228),
                sender_comp_id: f
                    .and_then(|f| f.fix_key_id.clone())
                    .unwrap_or_else(|| signer.key_id().to_string()),
                target_comp_id: f
                    .map(|f| f.target_comp_id.clone())
                    .unwrap_or_else(|| "KalshiNR".into()),
                heartbeat_secs: f.map(|f| f.heartbeat_secs).unwrap_or(10),
                tls: true,
            };
            tracing::info!(host = %fix_cfg.host, port = fix_cfg.port, "FIX order entry: connecting");
            // First build the stable request map from the exact FIX config;
            // the immutable pre-write hook then owns exact framed bytes.
            let prestaged = prestage_orders(
                order_run_identity,
                &cfg.keywords,
                earshot::kalshi::transport::StageWire::Fix(&fix_cfg),
            );
            let pre_write_hook = order_evidence_sink.as_ref().map(|sink| {
                Arc::new(FixOrderEvidenceHook::new(sink.clone(), &prestaged))
                    as Arc<dyn earshot::kalshi::fix::transport::FixPreWriteHook>
            });
            let fix = if let Some(run_plan) = verified_run_plan.as_ref() {
                let endpoint_id = format!(
                    "{}://{}:{}|sender={}|target={}",
                    if fix_cfg.tls { "tls" } else { "tcp" },
                    fix_cfg.host,
                    fix_cfg.port,
                    fix_cfg.sender_comp_id,
                    fix_cfg.target_comp_id,
                );
                let started = FixObservationTapeTask::start(
                    &run_plan.fix_observation_tape_path,
                    FixObservationTapeIdentity {
                        run_id: run_plan.plan.run_id.clone(),
                        event_id: run_plan.plan.event_id.clone(),
                        kalshi_env: run_plan.plan.kalshi_env.clone(),
                        account_fingerprint: run_plan.plan.account_fingerprint.clone(),
                        plan_sha256: run_plan.plan.plan_sha256.clone(),
                        endpoint_id,
                    },
                    FixObservationTapeOptions::default(),
                )
                .await;
                let (tap, mut tape) = match started {
                    Ok(started) => started,
                    Err(error) => {
                        let order_close = close_order_evidence_before_activation!();
                        gate.flush();
                        return Err(format!(
                            "FIX observation tape refused before activation: {error}; order_evidence_close={order_close:?}"
                        )
                        .into());
                    }
                };
                fix_observation_failures = Some(tape.subscribe_failures());
                fix_observation_health = Some(tape.health());
                let connect =
                    earshot::kalshi::fix::transport::FixTransport::connect_with_mandatory_observer(
                        fix_cfg,
                        signer.clone(),
                        Duration::from_secs(5),
                        pre_write_hook,
                        tap,
                    )
                    .await;
                match connect {
                    Ok(fix) => {
                        fix_observation_tape = Some(tape);
                        fix
                    }
                    Err(error) => {
                        let _ = tape.request_close(FixObservationTapeCloseReason::EvidenceFault);
                        let _ = tokio::time::timeout(Duration::from_secs(7), tape.wait()).await;
                        let order_close = close_order_evidence_before_activation!();
                        gate.flush();
                        return Err(format!(
                            "FIX logon failed after observation banker opened (bank finalized partial): {error}; order_evidence_close={order_close:?}"
                        )
                        .into());
                    }
                }
            } else {
                match pre_write_hook {
                    Some(hook) => {
                        earshot::kalshi::fix::transport::FixTransport::connect_with_pre_write_hook(
                            fix_cfg,
                            signer.clone(),
                            Duration::from_secs(5),
                            hook,
                        )
                        .await
                    }
                    None => {
                        earshot::kalshi::fix::transport::FixTransport::connect(
                            fix_cfg,
                            signer.clone(),
                            Duration::from_secs(5),
                        )
                        .await
                    }
                }
                .map_err(|e| format!("FIX logon failed (refusing to arm): {e}"))?
            };
            fix_transport_owner = Some(fix.clone());
            (OrderTransport::Fix { fix }, prestaged)
        }
    };

    // Start background ledger persistence only after every validation,
    // source-graph construction, and order-transport connection step that can
    // return without the common recorder cleanup tail. From here onward each
    // startup refusal explicitly aborts it after finalizing opened evidence.
    let persister = gate.spawn_persister(&tokio::runtime::Handle::current());

    // Live runs bank the exact WS frames used to maintain the executor's
    // BookCache. The tape header is opened exclusively and synced before the
    // tapped feed starts. Dry-run keeps the original unrecorded reader.
    let book_clock_offset_s = if verified_run_plan.is_some() {
        match rest.clock_offset_estimate().await {
            Ok(offset) => Some(offset),
            Err(error) => {
                drop(transport);
                let (_, fix_fault) = finalize_fix_observation_tape(
                    fix_observation_tape.take(),
                    fix_transport_owner.take(),
                    FixObservationTapeCloseReason::EvidenceFault,
                )
                .await;
                let order_close = close_order_evidence_before_activation!();
                gate.flush();
                persister.abort();
                return Err(format!(
                    "book clock-offset preflight failed: {error}; FIX evidence shutdown: {}; order_evidence_close={order_close:?}",
                    fix_fault.unwrap_or_else(|| "not applicable/clean".to_string()),
                )
                .into());
            }
        }
    } else {
        None
    };
    let (mut book_tape, mut book_tape_failures, ws) = if let Some(run_plan) =
        verified_run_plan.as_ref()
    {
        let started = BookTapeTask::start_new(
            &run_plan.book_tape_path,
            BookTapeSession {
                endpoint: kalshi_env.ws_url.to_string(),
                event: Some(run_plan.plan.market_event_ticker.clone()),
                label: run_plan.plan.run_id.clone(),
                tickers: tickers.clone(),
                host: run_plan.plan.execution_host_id.clone(),
                git_sha: option_env!("EARSHOT_BUILD_GIT_SHA")
                    .unwrap_or("unknown")
                    .to_string(),
                clock_offset_s: book_clock_offset_s,
                planned_secs: None,
            },
            cache.clone(),
            BOOK_TAPE_CAPACITY,
            BookTapeOptions::default(),
        )
        .await;
        let (tap, tape) = match started {
            Ok(started) => started,
            Err(error) => {
                drop(transport);
                let (_, fix_fault) = finalize_fix_observation_tape(
                    fix_observation_tape.take(),
                    fix_transport_owner.take(),
                    FixObservationTapeCloseReason::EvidenceFault,
                )
                .await;
                let order_close = close_order_evidence_before_activation!();
                gate.flush();
                persister.abort();
                return Err(format!(
                        "book tape refused before activation: {error}; FIX evidence shutdown: {}; order_evidence_close={order_close:?}",
                        fix_fault.unwrap_or_else(|| "not applicable/clean".to_string()),
                    )
                    .into());
            }
        };
        let failures = tape.subscribe_failures();
        let ws = tokio::spawn(run_ws_url_tapped(
            kalshi_env.ws_url.to_string(),
            signer.clone(),
            cache.clone(),
            tickers.clone(),
            Some(tap),
        ));
        tracing::info!(
            path = %run_plan.book_tape_path.display(),
            capacity = BOOK_TAPE_CAPACITY,
            "RunPlan book tape opened durably before WebSocket feed"
        );
        (Some(tape), Some(failures), ws)
    } else {
        let ws = tokio::spawn(run_ws(
            kalshi_env.clone(),
            signer.clone(),
            cache.clone(),
            tickers.clone(),
        ));
        (None, None, ws)
    };

    enum BookStartup {
        Books(Result<(), AnyErr>),
        WriterFailed(String),
        WriterCompleted(Result<BookTapeSummary, String>),
    }
    let book_startup = tokio::select! {
        result = wait_for_books(&cache, &tickers, Duration::from_secs(15)) => {
            BookStartup::Books(result)
        }
        detail = wait_for_book_tape_failure(&mut book_tape_failures) => {
            BookStartup::WriterFailed(detail)
        }
        completed = wait_for_book_tape_completion(&mut book_tape) => {
            BookStartup::WriterCompleted(completed)
        }
    };
    match book_startup {
        BookStartup::Books(Ok(())) => {
            tracing::info!("book snapshots live for all armed markets");
        }
        BookStartup::Books(Err(error)) => {
            let (_, terminal) = finalize_book_feed(ws, book_tape, true).await;
            drop(transport);
            let (_, fix_fault) = finalize_fix_observation_tape(
                fix_observation_tape.take(),
                fix_transport_owner.take(),
                FixObservationTapeCloseReason::EvidenceFault,
            )
            .await;
            let order_close = close_order_evidence_before_activation!();
            gate.flush();
            persister.abort();
            return Err(format!(
                "{error}; book evidence shutdown: {}; FIX evidence shutdown: {}; order_evidence_close={order_close:?}",
                terminal.unwrap_or_else(|| "closed cleanly".to_string()),
                fix_fault.unwrap_or_else(|| "not applicable/clean".to_string()),
            )
            .into());
        }
        BookStartup::WriterFailed(detail) => {
            let (_, terminal) = finalize_book_feed(ws, book_tape, false).await;
            drop(transport);
            let (_, fix_fault) = finalize_fix_observation_tape(
                fix_observation_tape.take(),
                fix_transport_owner.take(),
                FixObservationTapeCloseReason::EvidenceFault,
            )
            .await;
            let order_close = close_order_evidence_before_activation!();
            gate.flush();
            persister.abort();
            return Err(format!(
                "book tape writer failed before activation: {detail}; terminal: {}; FIX evidence shutdown: {}; order_evidence_close={order_close:?}",
                terminal.unwrap_or_else(|| "writer stopped without a terminal fault detail".to_string()),
                fix_fault.unwrap_or_else(|| "not applicable/clean".to_string()),
            )
            .into());
        }
        BookStartup::WriterCompleted(completed) => {
            let detail = premature_book_tape_completion(&completed);
            let (_, terminal) = finalize_book_feed(ws, book_tape, false).await;
            drop(transport);
            let (_, fix_fault) = finalize_fix_observation_tape(
                fix_observation_tape.take(),
                fix_transport_owner.take(),
                FixObservationTapeCloseReason::EvidenceFault,
            )
            .await;
            let order_close = close_order_evidence_before_activation!();
            gate.flush();
            persister.abort();
            return Err(format!(
                    "{detail}; terminal: {}; FIX evidence shutdown: {}; order_evidence_close={order_close:?}",
                    terminal.unwrap_or_else(
                        || "writer stopped without a terminal fault detail".to_string()
                    ),
                    fix_fault.unwrap_or_else(|| "not applicable/clean".to_string()),
                )
            .into());
        }
    }

    // Open audio evidence last among the fallible live dependencies, but
    // still before the pipeline's startup door can release any ingress. A
    // failure here explicitly closes every recorder already opened.
    let (mut audio_evidence_bank, audio_evidence_tap) = if let Some(run_plan) =
        verified_run_plan.as_ref()
    {
        let mut source_labels = std::collections::BTreeMap::new();
        for (index, source) in cfg.sources.iter().enumerate() {
            source_labels.insert(index as u16, source_label(source));
        }
        let mut audio_config = AudioEvidenceConfig::new(
            AudioEvidenceIdentity {
                run_id: run_plan.plan.run_id.clone(),
                event_id: run_plan.plan.event_id.clone(),
                plan_sha256: run_plan.plan.plan_sha256.clone(),
            },
            AudioEvidencePaths {
                raw: run_plan.audio_raw_path.clone(),
                frames: run_plan.audio_frames_path.clone(),
                manifest: run_plan.audio_manifest_path.clone(),
                commit: run_plan.audio_commit_path.clone(),
                lock: run_plan.audio_lock_path.clone(),
                wav_dir: run_plan.audio_wav_dir.clone(),
            },
        );
        audio_config.source_labels = source_labels;
        match AudioEvidenceBank::start_new(audio_config) {
            Ok((bank, tap)) => {
                tracing::info!(
                    raw = %run_plan.audio_raw_path.display(),
                    "RunPlan audio evidence opened durably before live ingress"
                );
                (Some(bank), Some(tap))
            }
            Err(error) => {
                let (_, book_fault) = finalize_book_feed(ws, book_tape, true).await;
                drop(transport);
                let (_, fix_fault) = finalize_fix_observation_tape(
                    fix_observation_tape.take(),
                    fix_transport_owner.take(),
                    FixObservationTapeCloseReason::EvidenceFault,
                )
                .await;
                let order_close = close_order_evidence_before_activation!();
                gate.flush();
                persister.abort();
                return Err(format!(
                        "audio evidence refused before activation: {error}; book shutdown: {}; FIX shutdown: {}; order_evidence_close={order_close:?}",
                        book_fault.unwrap_or_else(|| "clean".to_string()),
                        fix_fault.unwrap_or_else(|| "not applicable/clean".to_string()),
                    )
                    .into());
            }
        }
    } else {
        (None, None)
    };

    // The library convenience method detaches its refresh loop. Runtime
    // assembly instead owns this equivalent handle and starts it only after
    // the last fallible recorder open, so every teardown path can abort it.
    let presigner_refresher = rest_presigner_for_refresher
        .take()
        .map(spawn_owned_presigner_refresher);

    // Keep the REST orders connection hot: the fire path under `rest`, and
    // the ambiguous-status/cancel path under either wire.
    let conn_warmer = rest.spawn_conn_warmer(&tokio::runtime::Handle::current());

    // Executor consuming the decision stage's intent channel.
    let haircuts: HashMap<u16, u32> = cfg
        .keywords
        .iter()
        .enumerate()
        .map(|(i, k)| (i as u16, k.rule_risk_haircut_cents as u32))
        .collect();
    let (intent_tx, intent_rx) = tokio::sync::mpsc::channel(64);
    let registry = earshot::kalshi::OrderRegistry::default();
    let keyword_text = cfg
        .keywords
        .iter()
        .enumerate()
        .map(|(index, keyword)| (index as u16, keyword.phrase.clone()))
        .collect();
    let rest_endpoint_id = rest.endpoint_id();
    let ctx = Arc::new(ExecCtx {
        transport,
        rest: rest.clone(),
        cache: cache.clone(),
        gate: gate.clone(),
        prestaged,
        haircuts,
        dry_run: !i_am_live,
        registry: registry.clone(),
        order_evidence: order_evidence_sink,
        decision_plan_sha1,
        keyword_text,
        rest_endpoint_id,
    });
    // Executor on its own OS thread + current-thread runtime: the mpsc send
    // from the detect thread wakes THIS parked thread directly instead of
    // queueing through the shared 2-worker runtime (which also serves WS,
    // presign refresh, warmers — any of which could hold both workers at the
    // wrong instant). Pinned/RT per [perf] on a tuned box; plain thread
    // otherwise. spawn_blocking keeps the join awaitable from arm.
    let perf = cfg.perf;
    let exec_task = tokio::task::spawn_blocking(move || {
        earshot::perf::apply_to_current_thread(
            "executor",
            perf.core_executor,
            perf.fifo_priority(),
        );
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("executor runtime")
            .block_on(run_executor(intent_rx, ctx))
    });

    // Kill switch: STOP, SIGINT, and SIGTERM all return a typed event to this
    // assembly task. The risk watcher has already killed the gate, cancelled
    // only this session's orders, and flushed the ledger when delivery occurs;
    // it never exits the process. Assembly acknowledges only after every
    // recorder and the final report have completed.
    let rest_for_kill = rest.clone();
    let registry_for_kill = registry.clone();
    let source_cancellation_for_kill = source_cancellation.clone();
    let (mut watcher, mut kill_event_rx) =
        spawn_coordinated_kill_watcher(gate.clone(), state_dir.join("STOP"), move || {
            let rest = rest_for_kill.clone();
            let registry = registry_for_kill.clone();
            let source_cancellation = source_cancellation_for_kill.clone();
            async move {
                // Stop ingress at the same typed kill boundary, before the
                // bounded exact-ID cancellation loop. Event delivery waits
                // for that loop, so deferring this to assembly would admit up
                // to 15 seconds of pointless post-kill source traffic.
                source_cancellation.cancel();
                let (n, unresolved_client_ids) = kill_and_cancel_session(&rest, &registry).await;
                tracing::info!(
                    cancelled = n,
                    observed_orders = registry.len(),
                    unresolved_client_ids = unresolved_client_ids.len(),
                    "kill: session orders cancelled/reconciled"
                );
                (n, unresolved_client_ids)
            }
        });

    let table = decision_table;
    let admission = decision_admission;
    let event_id = cfg.event.id.clone();
    let chunk_ms = cfg.pipeline.chunk_ms;
    let preproc_cfg = PreprocCfg {
        resample_to_hz: cfg.pipeline.resample_to_hz,
    };
    // One-shot pre-warm at t=0 of listening: wait_for_books may have burned up
    // to 15 s, so guarantee a hot connection at the instant the pipeline starts.
    rest.warm().await;
    // Shadow warm-fire: a signed GET on the orders origin for an id that
    // cannot exist. Places nothing, debits nothing — but walks the same
    // signed-request build + pooled-h2 send + JSON-parse machinery as the
    // real fire, so the FIRST live order doesn't pay cold i-cache/branch
    // state on that code (measured by perf_cold_first_fire).
    match rest
        .order_by_client_id("earshot-warmfire-never-an-order")
        .await
    {
        Ok(None) => tracing::debug!("warm-fire ok (no such order, as designed)"),
        Ok(Some(_)) => tracing::warn!("warm-fire id unexpectedly matched an order — ignoring"),
        Err(e) => tracing::warn!("warm-fire query failed (non-fatal): {e}"),
    }
    // Remembered before `built` moves: a fanout pipeline only returns Err once
    // EVERY leg has died (pipeline/mod.rs), which is a different diagnosis from
    // a single source failing, and the report should say which.
    let is_fanout = matches!(built, BuiltPipeline::Fanout { .. });
    // Spawn the graph behind a one-use startup door.  The thread exists (so
    // readiness does not lie about scheduling) but cannot consume one frame
    // or emit one intent until assembly has durably written the readiness
    // receipt and, for live mode, armed the risk gate.  Sending `Err` closes
    // the graph cleanly when the receipt cannot be written.
    let (pipeline_start_tx, pipeline_start_rx) = std::sync::mpsc::channel::<Result<(), String>>();
    let mut pipeline_task = tokio::task::spawn_blocking(move || {
        match pipeline_start_rx.recv() {
            Ok(Ok(())) => {}
            Ok(Err(detail)) => {
                return RuntimePipelineResult::plain(Err(PipelineFailure::startup(detail)))
            }
            Err(error) => {
                return RuntimePipelineResult::plain(Err(PipelineFailure::startup(format!(
                    "assembly dropped startup door: {error}"
                ))))
            }
        }
        match (built, audio_evidence_tap) {
            (BuiltPipeline::Single { source, detector }, Some(tap)) => {
                RuntimePipelineResult::audio(run_pipeline_admitted_with_audio(
                    &event_id,
                    chunk_ms,
                    preproc_cfg,
                    source,
                    detector,
                    table,
                    Some(intent_tx),
                    perf,
                    admission,
                    tap,
                ))
            }
            (BuiltPipeline::Fanout { legs }, Some(tap)) => {
                RuntimePipelineResult::audio(run_pipeline_fanout_admitted_with_audio(
                    &event_id,
                    chunk_ms,
                    preproc_cfg,
                    legs,
                    table,
                    Some(intent_tx),
                    perf,
                    admission,
                    tap,
                ))
            }
            (BuiltPipeline::Single { source, detector }, None) => {
                RuntimePipelineResult::plain(run_pipeline_admitted(
                    &event_id,
                    chunk_ms,
                    preproc_cfg,
                    source,
                    detector,
                    table,
                    Some(intent_tx),
                    perf,
                    admission,
                ))
            }
            (BuiltPipeline::Fanout { legs }, None) => {
                RuntimePipelineResult::plain(run_pipeline_fanout_admitted(
                    &event_id,
                    chunk_ms,
                    preproc_cfg,
                    legs,
                    table,
                    Some(intent_tx),
                    perf,
                    admission,
                ))
            }
        }
    });
    // Readiness token (phone lane's dial gate): written ONLY here — after
    // build_pipeline_input returned (detector fully constructed, past the
    // exact point where the 2026-07-29 ORT hang lived) AND the pipeline
    // thread is spawned. The Twilio listener binds long before the detector
    // is built, so an open port (`nc -z`) proves nothing; launch-event.sh
    // gates the dial on THIS file instead. Written unconditionally (port 0
    // when the config has no Twilio source — the launcher only consumes it
    // for phone events); never written by the replay path.
    let readiness = write_listener_ready(state_dir, twilio_port.unwrap_or(0));
    let startup_signal_error = match readiness {
        Ok(()) => {
            // This is the final live activation step. Every operation after
            // it is infallible or is routed through the teardown/report tail.
            if let Some(max_cap) = live_max_cap {
                gate.arm();
                if fillable {
                    tracing::warn!(
                        max_cap_cents = max_cap,
                        "ARMED for live orders — MENTION-FILL posture (allowlist, Mentions permitted, orders MAY cross the ask up to the fill ceiling; real spend bounded by notional caps + $1 ledger)"
                    );
                } else if allow_mention {
                    tracing::warn!(
                        "ARMED for live orders — MENTION-PROBE posture (allowlist, Mentions permitted, cap ≤ 1¢, $1 ledger)"
                    );
                } else {
                    tracing::warn!(
                        "ARMED for live orders (M2 gates: allowlist, non-mention, cap ≤ 1¢)"
                    );
                }
            }
            pipeline_start_tx.send(Ok(())).err().map(|error| {
                let detail = format!("failed to release spawned pipeline: {error}");
                gate.kill();
                let _ = std::fs::remove_file(state_dir.join(LISTENER_READY_FILE));
                detail
            })
        }
        Err(error) => {
            let detail = error.to_string();
            // The receiver owns every source and the only intent sender.  A
            // successful refusal therefore drops both without starting them.
            let _ = pipeline_start_tx.send(Err(detail.clone()));
            Some(detail)
        }
    };
    // Deliberately NOT `??`. The pipeline's outcome decides the exit REASON;
    // it must never decide whether a report exists. Before 2026-08-05 an
    // errored pipeline skipped every line below and the run vanished (MELI).
    let decision_health_after_select = decision_health.clone();
    let order_evidence_health_after_select = order_evidence_health.clone();
    let fix_observation_health_after_select = fix_observation_health.clone();
    let mut handled_decision_fault: Option<String> = None;
    let mut handled_book_tape_fault: Option<String> = None;
    let mut handled_audio_evidence_fault: Option<String> = None;
    let mut handled_fix_observation_fault: Option<String> = None;
    let mut handled_kill: Option<CoordinatedKill<(usize, Vec<String>)>> = None;
    let mut decision_fault_wait = Box::pin(wait_for_decision_fault(decision_health));
    let mut order_evidence_fault_wait = Box::pin(async move {
        match order_evidence_health {
            Some(health) => wait_for_order_evidence_fault(health).await,
            None => std::future::pending::<String>().await,
        }
    });
    let mut audio_evidence_fault_wait = Box::pin(wait_for_audio_evidence_fault(
        audio_evidence_bank.as_ref(),
        state_dir,
        verified_run_plan.as_ref(),
    ));
    let (pipeline_outcome, mut cancellation_confirmed) = tokio::select! {
        biased;
        event = &mut kill_event_rx => {
            match event {
                Ok(event) => {
                    source_cancellation.cancel();
                    let unresolved_order_ids: HashSet<String> = event
                        .cancellation
                        .1
                        .iter()
                        .filter_map(|id| id.strip_prefix("order_id:").map(str::to_owned))
                        .collect();
                    let attempted: HashSet<String> = registry
                        .ids()
                        .into_iter()
                        .filter(|id| !unresolved_order_ids.contains(id))
                        .collect();
                    handled_kill = Some(event);
                    await_pipeline_with_fault_cancellation(
                        &mut pipeline_task,
                        &rest,
                        &registry,
                        attempted,
                    )
                    .await
                }
                Err(error) => {
                    let detail = format!(
                        "coordinated kill watcher disappeared before event delivery: {error}"
                    );
                    source_cancellation.cancel();
                    let cancellation_attempted = contain_book_tape_fault(
                        state_dir,
                        &detail,
                        &gate,
                        &rest,
                        &registry,
                    )
                    .await;
                    handled_decision_fault = Some(detail);
                    await_pipeline_with_fault_cancellation(
                        &mut pipeline_task,
                        &rest,
                        &registry,
                        cancellation_attempted,
                    )
                    .await
                }
            }
        }
        outcome = &mut pipeline_task => (outcome, HashSet::new()),
        detail = &mut decision_fault_wait => {
            // Persistence failed before an intent could leave the decision
            // layer. Make that a session kill immediately: refuse queued
            // intents, cancel prior session orders, remove the redial door,
            // and bank a structured receipt while the audio graph drains.
            source_cancellation.cancel();
            let cancellation_attempted = contain_decision_fault(
                state_dir,
                &cfg.event.id,
                decision_journal_path.as_deref(),
                &detail,
                &gate,
                &rest,
                &registry,
            )
            .await;
            handled_decision_fault = Some(detail);
            await_pipeline_with_fault_cancellation(
                &mut pipeline_task,
                &rest,
                &registry,
                cancellation_attempted,
            )
            .await
        }
        detail = &mut order_evidence_fault_wait => {
            let detail = format!("order evidence durability failure: {detail}");
            source_cancellation.cancel();
            let cancellation_attempted = contain_decision_fault(
                state_dir,
                &cfg.event.id,
                decision_journal_path.as_deref(),
                &detail,
                &gate,
                &rest,
                &registry,
            )
            .await;
            handled_decision_fault = Some(detail);
            await_pipeline_with_fault_cancellation(
                &mut pipeline_task,
                &rest,
                &registry,
                cancellation_attempted,
            )
            .await
        }
        detail = wait_for_book_tape_failure(&mut book_tape_failures) => {
            let detail = format!("book tape evidence failure: {detail}");
            // Stop the producer immediately. The tape writer deliberately
            // stays alive until this drops the final FrameTap and accounts for
            // every accepted/unwritten frame.
            ws.abort();
            source_cancellation.cancel();
            let cancellation_attempted = contain_book_tape_fault(
                state_dir,
                &detail,
                &gate,
                &rest,
                &registry,
            )
            .await;
            handled_book_tape_fault = Some(detail);
            await_pipeline_with_fault_cancellation(
                &mut pipeline_task,
                &rest,
                &registry,
                cancellation_attempted,
            )
            .await
        }
        completed = wait_for_book_tape_completion(&mut book_tape) => {
            let detail = premature_book_tape_completion(&completed);
            ws.abort();
            source_cancellation.cancel();
            let cancellation_attempted = contain_book_tape_fault(
                state_dir,
                &detail,
                &gate,
                &rest,
                &registry,
            )
            .await;
            handled_book_tape_fault = Some(detail);
            await_pipeline_with_fault_cancellation(
                &mut pipeline_task,
                &rest,
                &registry,
                cancellation_attempted,
            )
            .await
        }
        detail = &mut audio_evidence_fault_wait => {
            let detail = format!("audio evidence failure: {detail}");
            source_cancellation.cancel();
            let cancellation_attempted = contain_book_tape_fault(
                state_dir,
                &detail,
                &gate,
                &rest,
                &registry,
            )
            .await;
            handled_audio_evidence_fault = Some(detail);
            await_pipeline_with_fault_cancellation(
                &mut pipeline_task,
                &rest,
                &registry,
                cancellation_attempted,
            )
            .await
        }
        detail = wait_for_fix_observation_failure(&mut fix_observation_failures) => {
            let detail = format!("FIX observation evidence failure: {detail}");
            source_cancellation.cancel();
            let cancellation_attempted = contain_book_tape_fault(
                state_dir,
                &detail,
                &gate,
                &rest,
                &registry,
            )
            .await;
            handled_fix_observation_fault = Some(detail);
            await_pipeline_with_fault_cancellation(
                &mut pipeline_task,
                &rest,
                &registry,
                cancellation_attempted,
            )
            .await
        }
        completed = wait_for_fix_observation_completion(&mut fix_observation_tape) => {
            let detail = match completed {
                Ok(summary) => format!(
                    "FIX observation writer stopped before runtime drain: complete={} committed={} reason={}",
                    summary.complete, summary.committed, summary.reason
                ),
                Err(error) => format!("FIX observation writer failed before runtime drain: {error}"),
            };
            source_cancellation.cancel();
            let cancellation_attempted = contain_book_tape_fault(
                state_dir,
                &detail,
                &gate,
                &rest,
                &registry,
            )
            .await;
            handled_fix_observation_fault = Some(detail);
            await_pipeline_with_fault_cancellation(
                &mut pipeline_task,
                &rest,
                &registry,
                cancellation_attempted,
            )
            .await
        }
    };
    // Release the monitor's immutable borrow before taking/finalizing the
    // bank below. `AudioEvidenceHealth` is a snapshot, so all post-select
    // checks must call `bank.health()` again rather than reuse a clone.
    drop(audio_evidence_fault_wait);
    if handled_audio_evidence_fault.is_none() {
        if let Ok(outcome) = &pipeline_outcome {
            if let Some(detail) = outcome.audio_evidence_error.as_deref() {
                let detail = format!("audio evidence forwarding failure: {detail}");
                source_cancellation.cancel();
                cancellation_confirmed.extend(
                    contain_book_tape_fault(state_dir, &detail, &gate, &rest, &registry).await,
                );
                handled_audio_evidence_fault = Some(detail);
            }
        }
    }
    // `tokio::select!` may choose the pipeline branch when both futures become
    // ready together. Re-check the shared health door so that race can never
    // skip the gate kill, cancellation, or fault receipt.
    if handled_decision_fault.is_none() {
        if let Some(detail) = decision_health_after_select.fault_detail() {
            source_cancellation.cancel();
            cancellation_confirmed.extend(
                contain_decision_fault(
                    state_dir,
                    &cfg.event.id,
                    decision_journal_path.as_deref(),
                    &detail,
                    &gate,
                    &rest,
                    &registry,
                )
                .await,
            );
            handled_decision_fault = Some(detail);
        } else if let Some(detail) = order_evidence_health_after_select
            .as_ref()
            .and_then(|health| health.fault_detail())
        {
            let detail = format!("order evidence durability failure: {detail}");
            source_cancellation.cancel();
            cancellation_confirmed.extend(
                contain_decision_fault(
                    state_dir,
                    &cfg.event.id,
                    decision_journal_path.as_deref(),
                    &detail,
                    &gate,
                    &rest,
                    &registry,
                )
                .await,
            );
            handled_decision_fault = Some(detail);
        }
    }
    if handled_audio_evidence_fault.is_none() {
        if let Some(health) = audio_evidence_bank.as_ref().map(AudioEvidenceBank::health) {
            let detail =
                persist_source_attached_marker(&health, state_dir, verified_run_plan.as_ref())
                    .err()
                    .or_else(|| {
                        health
                            .poisoned
                            .then(|| format_audio_evidence_health_fault(&health))
                    });
            if let Some(detail) = detail {
                source_cancellation.cancel();
                if handled_decision_fault.is_none() {
                    cancellation_confirmed.extend(
                        contain_book_tape_fault(state_dir, &detail, &gate, &rest, &registry).await,
                    );
                }
                handled_audio_evidence_fault = Some(detail);
            }
        }
    }
    if handled_fix_observation_fault.is_none() {
        let detail = fix_observation_health_after_select
            .as_ref()
            .and_then(FixObservationTapeHealth::fault_detail)
            .or_else(|| {
                fix_observation_tape
                    .as_ref()
                    .is_some_and(FixObservationTapeHandle::is_finished)
                    .then(|| "FIX observation writer stopped before runtime drain".to_string())
            });
        if let Some(detail) = detail {
            source_cancellation.cancel();
            if handled_decision_fault.is_none() && handled_audio_evidence_fault.is_none() {
                cancellation_confirmed.extend(
                    contain_book_tape_fault(state_dir, &detail, &gate, &rest, &registry).await,
                );
            }
            handled_fix_observation_fault = Some(detail);
        }
    }
    // The pipeline and recorder may become ready in the same scheduler turn.
    // Re-read both recorder doors after select so a clean pipeline branch can
    // never mask an already-published evidence failure or early writer exit.
    if handled_book_tape_fault.is_none() {
        let detail = book_tape_failures
            .as_ref()
            .and_then(|failures| failures.borrow().clone())
            .or_else(|| {
                book_tape
                    .as_ref()
                    .is_some_and(BookTapeHandle::is_finished)
                    .then(|| "book tape writer stopped before runtime drain".to_string())
            });
        if let Some(detail) = detail {
            ws.abort();
            source_cancellation.cancel();
            if handled_decision_fault.is_none() {
                cancellation_confirmed.extend(
                    contain_book_tape_fault(state_dir, &detail, &gate, &rest, &registry).await,
                );
            }
            handled_book_tape_fault = Some(detail);
        }
    }

    // Pipeline finished or died → `intent_tx` dropped either way → the
    // executor drains and returns whatever it managed to do.
    let exec_outcome = exec_task.await;
    if handled_decision_fault.is_none() {
        if let Some(detail) = order_evidence_health_after_select
            .as_ref()
            .and_then(|health| health.fault_detail())
        {
            let detail =
                format!("order evidence durability failure after executor drain: {detail}");
            source_cancellation.cancel();
            cancellation_confirmed.extend(
                contain_decision_fault(
                    state_dir,
                    &cfg.event.id,
                    decision_journal_path.as_deref(),
                    &detail,
                    &gate,
                    &rest,
                    &registry,
                )
                .await,
            );
            handled_decision_fault = Some(detail);
        }
    }
    // If the pipeline completed in the same scheduler turn as a kill event,
    // the biased select above normally observes kill first. Recheck the risk
    // gate and receiver before aborting the normal-run watcher. Internal
    // evidence containment also kills the gate, so only a kill with no known
    // internal cause waits for the watcher's bounded 15-second reconciliation.
    let internal_kill = startup_signal_error.is_some()
        || handled_decision_fault.is_some()
        || handled_book_tape_fault.is_some()
        || handled_audio_evidence_fault.is_some()
        || handled_fix_observation_fault.is_some();
    if handled_kill.is_none() && gate.is_killed() && !internal_kill {
        match tokio::time::timeout(Duration::from_secs(17), &mut kill_event_rx).await {
            Ok(Ok(event)) => {
                let unresolved_order_ids: HashSet<String> = event
                    .cancellation
                    .1
                    .iter()
                    .filter_map(|id| id.strip_prefix("order_id:").map(str::to_owned))
                    .collect();
                cancellation_confirmed.extend(
                    registry
                        .ids()
                        .into_iter()
                        .filter(|id| !unresolved_order_ids.contains(id)),
                );
                handled_kill = Some(event);
            }
            Ok(Err(error)) => {
                handled_decision_fault.get_or_insert_with(|| {
                    format!("coordinated kill event channel closed after gate kill: {error}")
                });
            }
            Err(_) => {
                handled_decision_fault.get_or_insert_with(|| {
                    "gate was killed but coordinated kill event was not delivered within 17 seconds"
                        .to_string()
                });
            }
        }
    }
    let needs_final_cancellation = internal_kill
        || handled_decision_fault.is_some()
        || handled_book_tape_fault.is_some()
        || handled_audio_evidence_fault.is_some()
        || handled_fix_observation_fault.is_some()
        || handled_kill.is_some();
    if needs_final_cancellation {
        // The first snapshot can race an order already inside transport that
        // registers its exchange id only as the executor drains. Reconcile one
        // final bounded time, preserving prior DELETE acknowledgements so an
        // idempotency response cannot turn a confirmed cancellation into a
        // false unresolved result.
        let (confirmed, unresolved) = kill_and_cancel_session_with_timeout(
            &rest,
            &registry,
            Duration::from_secs(2),
            cancellation_confirmed,
        )
        .await;
        cancellation_confirmed = confirmed;
        if let Some(event) = handled_kill.as_mut() {
            event.cancellation = (cancellation_confirmed.len(), unresolved.clone());
        }
        tracing::error!(
            cancelled = cancellation_confirmed.len(),
            unresolved = unresolved.len(),
            session_orders = registry.len(),
            "post-drain session cancellation reconciliation complete"
        );
    }
    if handled_kill.is_none() {
        watcher.abort();
    }
    // Book and FIX banking are independent after executor drain. Finalize
    // them concurrently so coordinated STOP/SIGTERM remains inside the
    // launcher's bounded teardown budget without weakening either proof.
    // Book still orders close intent -> producer join -> writer commit; FIX
    // still orders close intent -> Logout/shutdown -> banker commit.
    let request_book_close = handled_book_tape_fault.is_none();
    let fix_close_reason = match handled_kill.as_ref().map(|event| event.trigger) {
        Some(KillTrigger::Sigint) => FixObservationTapeCloseReason::Sigint,
        Some(KillTrigger::Sigterm) => FixObservationTapeCloseReason::Sigterm,
        Some(KillTrigger::StopFile) => FixObservationTapeCloseReason::Requested,
        None if handled_decision_fault.is_some()
            || handled_book_tape_fault.is_some()
            || handled_audio_evidence_fault.is_some()
            || handled_fix_observation_fault.is_some() =>
        {
            FixObservationTapeCloseReason::EvidenceFault
        }
        None => FixObservationTapeCloseReason::Requested,
    };
    let (
        (book_tape_summary, terminal_book_tape_fault),
        (fix_observation_summary, terminal_fix_observation_fault),
    ) = tokio::join!(
        finalize_book_feed(ws, book_tape, request_book_close),
        finalize_fix_observation_tape(
            fix_observation_tape.take(),
            fix_transport_owner.take(),
            fix_close_reason,
        ),
    );
    if let Some(detail) = terminal_book_tape_fault {
        if handled_book_tape_fault.is_none() {
            cancellation_confirmed.extend(
                contain_book_tape_fault_with_timeout(
                    state_dir,
                    &detail,
                    &gate,
                    &rest,
                    &registry,
                    Duration::from_secs(2),
                )
                .await,
            );
            handled_book_tape_fault = Some(detail);
        } else if let Some(current) = handled_book_tape_fault.as_mut() {
            current.push_str("; ");
            current.push_str(&detail);
        }
    }
    if let Some(detail) = terminal_fix_observation_fault {
        if let Some(current) = handled_fix_observation_fault.as_mut() {
            current.push_str("; ");
            current.push_str(&detail);
        } else {
            cancellation_confirmed.extend(
                contain_book_tape_fault_with_timeout(
                    state_dir,
                    &detail,
                    &gate,
                    &rest,
                    &registry,
                    Duration::from_secs(2),
                )
                .await,
            );
            handled_fix_observation_fault = Some(detail);
        }
    }
    if let Some(event) = handled_kill.as_mut() {
        let mut unresolved = registry.pending_client_ids();
        unresolved.extend(
            registry
                .ids()
                .into_iter()
                .filter(|id| !cancellation_confirmed.contains(id))
                .map(|id| format!("order_id:{id}")),
        );
        event.cancellation = (cancellation_confirmed.len(), unresolved);
    }
    let exec_report = exec_outcome.as_ref().ok();
    let pipeline_outcome = match pipeline_outcome {
        Ok(outcome) => Ok(outcome),
        Err(join) => Err(join),
    };
    let published_audio_frames = pipeline_outcome
        .as_ref()
        .ok()
        .and_then(RuntimePipelineResult::published_audio_frames);
    if let Ok(outcome) = &pipeline_outcome {
        if let Some(detail) = outcome.audio_evidence_error.as_deref() {
            handled_audio_evidence_fault.get_or_insert_with(|| detail.to_string());
        }
    }
    let pipeline_result_is_clean = pipeline_outcome
        .as_ref()
        .is_ok_and(|outcome| outcome.result.is_ok());
    let coordinated_kill_resolved = handled_kill
        .as_ref()
        .is_none_or(|event| event.cancellation.1.is_empty());
    let audio_finalize_clean = pipeline_result_is_clean
        && handled_decision_fault.is_none()
        && handled_book_tape_fault.is_none()
        && handled_audio_evidence_fault.is_none()
        && handled_fix_observation_fault.is_none()
        && coordinated_kill_resolved
        && exec_outcome.is_ok();
    let audio_abort_reason = handled_audio_evidence_fault
        .as_deref()
        .or(handled_decision_fault.as_deref())
        .or(handled_book_tape_fault.as_deref())
        .or(handled_fix_observation_fault.as_deref())
        .or_else(|| {
            handled_kill.as_ref().map(|event| match event.trigger {
                KillTrigger::StopFile => "stop_file",
                KillTrigger::Sigint => "sigint",
                KillTrigger::Sigterm => "sigterm",
            })
        })
        .unwrap_or(if published_audio_frames == Some(0) {
            "zero detector-visible audio frames"
        } else {
            "pipeline or executor did not drain cleanly"
        })
        .to_string();
    let audio_fault_before_finalize = handled_audio_evidence_fault.is_some();
    let audio_evidence_terminal = finalize_audio_evidence(
        audio_evidence_bank.take(),
        published_audio_frames,
        audio_finalize_clean,
        audio_abort_reason,
    )
    .await;
    match &audio_evidence_terminal {
        AudioEvidenceTerminal::Failed(detail) => {
            handled_audio_evidence_fault.get_or_insert_with(|| detail.clone());
        }
        AudioEvidenceTerminal::Partial(partial) if audio_finalize_clean => {
            handled_audio_evidence_fault.get_or_insert_with(|| {
                format!(
                    "audio evidence finalized partial after otherwise clean drain: reason={}; accepted={} written={} dropped={}",
                    partial.reason,
                    partial.accepted_frames,
                    partial.written_frames,
                    partial.dropped_frames,
                )
            });
        }
        AudioEvidenceTerminal::Committed(commit) if !commit.complete || !commit.durable => {
            handled_audio_evidence_fault.get_or_insert_with(|| {
                format!(
                    "audio evidence commit is unhealthy: complete={} durable={}",
                    commit.complete, commit.durable
                )
            });
        }
        _ => {}
    }
    if !audio_fault_before_finalize && handled_audio_evidence_fault.is_some() {
        let detail = handled_audio_evidence_fault
            .as_deref()
            .expect("checked above");
        cancellation_confirmed.extend(
            contain_book_tape_fault_with_timeout(
                state_dir,
                detail,
                &gate,
                &rest,
                &registry,
                Duration::from_secs(2),
            )
            .await,
        );
        if let Some(event) = handled_kill.as_mut() {
            let mut unresolved = registry.pending_client_ids();
            unresolved.extend(
                registry
                    .ids()
                    .into_iter()
                    .filter(|id| !cancellation_confirmed.contains(id))
                    .map(|id| format!("order_id:{id}")),
            );
            event.cancellation = (cancellation_confirmed.len(), unresolved);
        }
    }
    // All debits and terminal evidence outcomes are in. Force one final
    // durable ledger write, then stop background transport helpers.
    gate.flush();
    persister.abort();
    if let Some(refresher) = presigner_refresher {
        refresher.abort();
    }
    conn_warmer.abort();
    let decision_fault_detail = handled_decision_fault.as_deref();
    let book_tape_fault_detail = handled_book_tape_fault.as_deref();
    let order_evidence_fault = decision_fault_detail
        .is_some_and(|detail| detail.starts_with("order evidence durability failure"));
    let (report, exit, failure): (Option<PipelineReport>, ExitRecord, Option<AnyErr>) =
        match pipeline_outcome {
            Ok(RuntimePipelineResult {
                result: Ok(mut r), ..
            }) => {
                if let Some(e) = exec_report {
                    r.edges.extend(e.recorder.snapshot());
                }
                if let Some(detail) = decision_fault_detail {
                    r.decision_journal_error = Some(detail.to_string());
                    (
                        Some(r),
                        ExitRecord::with(
                            if order_evidence_fault {
                                ExitReason::OrderEvidenceError
                            } else {
                                ExitReason::DecisionStateError
                            },
                            detail,
                        ),
                        Some(std::io::Error::other(detail.to_string()).into()),
                    )
                } else {
                    (Some(r), ExitRecord::clean(), None)
                }
            }
            Ok(RuntimePipelineResult {
                result: Err(mut f), ..
            }) => {
                // Decision durability is a stage failure, never a source
                // diagnosis. Fanout source errors still surface only when
                // every leg is gone.
                let reason = if decision_fault_detail.is_some()
                    || f.kind == PipelineFailureKind::DecisionJournal
                {
                    if order_evidence_fault {
                        ExitReason::OrderEvidenceError
                    } else {
                        ExitReason::DecisionStateError
                    }
                } else if f.kind == PipelineFailureKind::Startup {
                    ExitReason::StartupError
                } else if is_fanout {
                    ExitReason::AllLegsDied
                } else {
                    ExitReason::SourceError
                };
                let detail = decision_fault_detail
                    .map(str::to_string)
                    .unwrap_or_else(|| f.to_string());
                let rec = ExitRecord::with(reason, detail);
                // A dead run still describes itself: fanout hands up the per-leg
                // frames/chunks/drops it had already accounted. Before 2026-08-07
                // this arm passed `None` and AAOI-26AUG06 wrote a report with no
                // frame count at all, which every downstream grader then read as
                // benign. Merge executor edges the same way the clean arm does.
                let partial = f.partial.take().map(|b| {
                    let mut r = *b;
                    if let Some(detail) = decision_fault_detail {
                        r.decision_journal_error = Some(detail.to_string());
                    }
                    if let Some(e) = exec_report {
                        r.edges.extend(e.recorder.snapshot());
                    }
                    r
                });
                (partial, rec, Some(Box::new(f) as AnyErr))
            }
            Err(join) => {
                let rec = if let Some(detail) = decision_fault_detail {
                    ExitRecord::with(
                        if order_evidence_fault {
                            ExitReason::OrderEvidenceError
                        } else {
                            ExitReason::DecisionStateError
                        },
                        format!("{detail}; concurrent pipeline join failure: {join}"),
                    )
                } else if let Some(detail) = startup_signal_error.as_deref() {
                    ExitRecord::with(ExitReason::StartupError, detail)
                } else {
                    ExitRecord::with(ExitReason::PanicOrJoin, join.to_string())
                };
                (None, rec, Some(Box::new(join) as AnyErr))
            }
        };

    // Until telemetry grows a transport-neutral evidence reason, the existing
    // durable-evidence exit is the closest fail-closed classification. The
    // report's book_tape_* fields preserve the precise failing subsystem.
    let (exit, failure) = if let Some(detail) = book_tape_fault_detail {
        (
            ExitRecord::with(ExitReason::OrderEvidenceError, detail),
            Some(std::io::Error::other(detail.to_string()).into()),
        )
    } else {
        (exit, failure)
    };
    let (exit, failure) = if let Some(detail) = handled_audio_evidence_fault.as_deref() {
        (
            ExitRecord::with(ExitReason::OrderEvidenceError, detail),
            Some(std::io::Error::other(detail.to_string()).into()),
        )
    } else {
        (exit, failure)
    };
    let (exit, failure) = if let Some(detail) = handled_fix_observation_fault.as_deref() {
        (
            ExitRecord::with(ExitReason::OrderEvidenceError, detail),
            Some(std::io::Error::other(detail.to_string()).into()),
        )
    } else {
        (exit, failure)
    };
    let (exit, failure) = if let Some(event) = handled_kill.as_ref() {
        let (cancelled, unresolved) = &event.cancellation;
        let detail = format!(
            "kill trigger={:?}; kill_to_cancel_ms={}; cancelled={}; unresolved_ids={:?}",
            event.trigger, event.kill_to_cancel_ms, cancelled, unresolved
        );
        let reason = match event.trigger {
            KillTrigger::Sigterm => ExitReason::Sigterm,
            KillTrigger::StopFile | KillTrigger::Sigint => ExitReason::StopFile,
        };
        let kill_failure =
            (!unresolved.is_empty()).then(|| std::io::Error::other(detail.clone()).into());
        if failure.is_none() {
            (ExitRecord::with(reason, &detail), kill_failure)
        } else {
            // Preserve the more specific evidence/source failure and its exit
            // classification. Kill metadata, including unresolved IDs, is
            // still serialized separately in the terminal report.
            (exit, failure.or(kill_failure))
        }
    } else {
        (exit, failure)
    };
    let (exit, failure) = if let Err(join) = &exec_outcome {
        let detail = format!("executor task failed: {join}");
        (
            ExitRecord::with(ExitReason::PanicOrJoin, &detail),
            Some(std::io::Error::other(detail).into()),
        )
    } else {
        (exit, failure)
    };
    let close_error = if let Some(journal) = order_evidence_journal {
        journal.close().err()
    } else {
        None
    };
    let (exit, failure) = if let Some(error) = close_error {
        let detail = format!("order evidence journal close failed: {error}");
        (
            ExitRecord::with(ExitReason::OrderEvidenceError, &detail),
            Some(std::io::Error::other(detail).into()),
        )
    } else {
        (exit, failure)
    };
    let report_result = finish_run(
        &report_path,
        &state_dir.join(SOURCE_ATTACHED_FILE),
        &cfg.event.id,
        report,
        exec_report,
        exit,
        verified_run_plan.as_ref(),
        order_evidence_health_after_select.as_ref(),
        book_tape_summary.as_ref(),
        book_tape_fault_detail,
        &audio_evidence_terminal,
        handled_audio_evidence_fault.as_deref(),
        fix_observation_summary.as_ref(),
        handled_fix_observation_fault.as_deref(),
        cfg.kalshi.transport == config::OrderWire::Fix,
        handled_kill.as_ref(),
    );
    if let Err(error) = report_result {
        // A report write is part of teardown, so it must never receive a
        // successful acknowledgement. Dropping the typed event sends the
        // watcher's explicit EventDropped disposition; await that bounded
        // release before returning the write error and letting the process
        // exit non-zero.
        if let Some(event) = handled_kill.take() {
            drop(event);
            let _ = tokio::time::timeout(Duration::from_secs(2), &mut watcher).await;
        }
        drop(decision_lease);
        return Err(error);
    }
    if let Some(event) = handled_kill.take() {
        let _cancellation = event.acknowledge();
        let _ = tokio::time::timeout(Duration::from_secs(2), &mut watcher).await;
    }
    // Keep the canonical journal lock until the executor has drained, the
    // ledger is flushed, and evidence writer shutdown is acknowledged.
    drop(decision_lease);

    match failure {
        // The report is on disk; now fail loudly so the exit code is honest.
        Some(e) => Err(e),
        None => match exec_outcome {
            Ok(_) => Ok(()),
            Err(join) => Err(Box::new(join) as AnyErr),
        },
    }
}

/// The ONE place an armed run's report reaches disk, and the one place its
/// summary is printed. Called on every terminated path — clean drain, source
/// error, all-legs-died, panic — so "no report" once again means "never
/// armed" rather than "died and told nobody".
///
/// `report` is `None` when the pipeline never produced one; the run still
/// records its event, its exit and any orders the executor managed, because a
/// partial record beats silence (2026-08-05 MELI).
fn nullable_required_health(required: bool, healthy: bool) -> serde_json::Value {
    if required {
        healthy.into()
    } else {
        serde_json::Value::Null
    }
}

fn terminal_runtime_health(
    reason: &ExitReason,
    has_run_plan: bool,
    book_healthy: bool,
    order_healthy: bool,
    audio_healthy: bool,
    fix_required: bool,
    fix_healthy: bool,
    coordinated_kill_resolved: Option<bool>,
) -> (bool, bool) {
    let required_evidence_healthy = !has_run_plan
        || (book_healthy && order_healthy && audio_healthy && (!fix_required || fix_healthy));
    let graceful_termination = matches!(reason, ExitReason::CleanDrain)
        || (matches!(reason, ExitReason::StopFile | ExitReason::Sigterm)
            && coordinated_kill_resolved == Some(true));
    (
        required_evidence_healthy,
        graceful_termination && required_evidence_healthy,
    )
}

fn finish_run(
    report_path: &Path,
    source_attached_path: &Path,
    event_id: &str,
    report: Option<PipelineReport>,
    exec: Option<&ExecReport>,
    exit: ExitRecord,
    run_plan: Option<&VerifiedRunPlan>,
    order_evidence_health: Option<&earshot::kalshi::order_evidence::OrderEvidenceHealth>,
    book_tape_summary: Option<&BookTapeSummary>,
    book_tape_fault: Option<&str>,
    audio_evidence: &AudioEvidenceTerminal,
    audio_evidence_fault: Option<&str>,
    fix_observation_summary: Option<&FixObservationTapeSummary>,
    fix_observation_fault: Option<&str>,
    fix_observation_required: bool,
    kill: Option<&CoordinatedKill<(usize, Vec<String>)>>,
) -> Result<(), AnyErr> {
    match &report {
        Some(r) => print!("\n{}", r.render_table()),
        None => println!(
            "\nno pipeline report — run ended as {:?}: {}",
            exit.reason,
            exit.detail.clone().unwrap_or_default()
        ),
    }
    match exec {
        Some(e) => {
            // Lead with what reached Kalshi. The old line printed
            // `orders.len()` — an INTENT count — and tagged every row [LIVE]
            // from `!dry_run`, so a run that sent nothing read as 23 orders
            // placed (2026-08-05). Never describe a posture by its flag.
            let sent = e.orders.iter().filter(|o| o.reached_wire()).count();
            let acked = e.orders.iter().filter(|o| o.acked()).count();
            println!(
                "\nintents {} — SENT to Kalshi {} ({} acked), {} never sent \
                 ({} LOST_RACE, {} observe non-crossing by design):",
                e.orders.len(),
                sent,
                acked,
                e.orders.len() - sent,
                e.lost_races,
                e.observe_noncross
            );
            for o in &e.orders {
                let wire = if o.dry_run {
                    "dry"
                } else if o.acked() {
                    "ACKED"
                } else if o.reached_wire() {
                    "SENT"
                } else {
                    "not-sent"
                };
                // A LOST_RACE sets neither refusal nor order_status and used to
                // print as an empty string — visually identical to an accepted
                // order with no status yet.
                let why = o
                    .refusal
                    .clone()
                    .or_else(|| o.order_status.clone())
                    .unwrap_or_else(|| {
                        if o.lost_race {
                            format!("no ask at or under {}¢", o.price_cents)
                        } else {
                            String::new()
                        }
                    });
                println!(
                    "  {} {} x{} @≤{}¢ [{}] {}",
                    o.client_order_id, o.ticker, o.count, o.price_cents, wire, why,
                );
            }
        }
        None => println!("\nexecutor produced no report (task failed) — orders unknown"),
    }

    let mut json = telemetry::build_run_json(event_id, report.as_ref(), exec, &exit)?;
    if let Some(run_plan) = run_plan {
        json["run_plan_schema"] = run_plan.plan.schema.into();
        json["run_id"] = run_plan.plan.run_id.clone().into();
        json["event_id"] = run_plan.plan.event_id.clone().into();
        json["plan_sha256"] = run_plan.plan.plan_sha256.clone().into();
        json["execution_host_id"] = run_plan.plan.execution_host_id.clone().into();
        json["account_fingerprint"] = run_plan.plan.account_fingerprint.clone().into();
        json["book_tape_path"] = run_plan.book_tape_path.display().to_string().into();
        json["order_evidence_path"] = run_plan.order_evidence_path.display().to_string().into();
        json["audio_raw_path"] = run_plan.audio_raw_path.display().to_string().into();
        json["audio_frames_path"] = run_plan.audio_frames_path.display().to_string().into();
        json["audio_manifest_path"] = run_plan.audio_manifest_path.display().to_string().into();
        json["audio_commit_path"] = run_plan.audio_commit_path.display().to_string().into();
        json["audio_lock_path"] = run_plan.audio_lock_path.display().to_string().into();
        json["audio_wav_dir"] = run_plan.audio_wav_dir.display().to_string().into();
        json["fix_observation_tape_path"] = run_plan
            .fix_observation_tape_path
            .display()
            .to_string()
            .into();
        json["source_attached_path"] = source_attached_path.display().to_string().into();
        json["source_attached"] = source_attached_path.exists().into();
        json["book_tape_healthy"] = (book_tape_fault.is_none()
            && book_tape_summary.is_some_and(|summary| summary.complete && summary.committed))
        .into();
    }
    if let Some(summary) = book_tape_summary {
        json["book_tape"] = serde_json::to_value(summary)?;
    }
    if let Some(detail) = book_tape_fault {
        json["book_tape_error"] = detail.to_string().into();
    }
    let order_healthy = order_evidence_health
        .map(|health| health.fault_detail().is_none())
        .unwrap_or(run_plan.is_none());
    json["order_evidence_healthy"] = order_healthy.into();
    let audio_healthy = matches!(audio_evidence, AudioEvidenceTerminal::Committed(commit)
        if commit.complete && commit.durable)
        && audio_evidence_fault.is_none();
    json["audio_evidence_healthy"] = audio_healthy.into();
    json["audio_evidence"] = match audio_evidence {
        AudioEvidenceTerminal::Committed(commit) => serde_json::json!({
            "status": "committed",
            "commit": commit,
        }),
        AudioEvidenceTerminal::Partial(partial) => serde_json::json!({
            "status": "partial",
            "partial": {
                "paths": partial.paths,
                "reason": partial.reason,
                "accepted_frames": partial.accepted_frames,
                "written_frames": partial.written_frames,
                "dropped_frames": partial.dropped_frames,
                "closed_attempts": partial.closed_attempts,
                "invalid_attempts": partial.invalid_attempts,
                "producer_quiesced": partial.producer_quiesced,
                "manifest_written": partial.manifest_written,
            },
        }),
        AudioEvidenceTerminal::Failed(detail) => serde_json::json!({
            "status": "failed",
            "error": detail,
        }),
        AudioEvidenceTerminal::NotApplicable => serde_json::json!({
            "status": "not_applicable",
        }),
    };
    if let Some(detail) = audio_evidence_fault {
        json["audio_evidence_error"] = detail.to_string().into();
    }
    let fix_expected = run_plan.is_some() && fix_observation_required;
    let fix_healthy = fix_observation_summary
        .is_some_and(|summary| summary.complete && summary.committed)
        && fix_observation_fault.is_none();
    json["fix_observation_evidence"] = match fix_observation_summary {
        Some(summary) => serde_json::json!({
            "status": if fix_healthy { "committed" } else { "partial" },
            "summary": summary,
        }),
        None if fix_expected => serde_json::json!({ "status": "missing" }),
        None => serde_json::json!({ "status": "not_applicable" }),
    };
    json["fix_observation_evidence_healthy"] = nullable_required_health(fix_expected, fix_healthy);
    json["fix_observation_tape_healthy"] = nullable_required_health(fix_expected, fix_healthy);
    if let Some(detail) = fix_observation_fault {
        json["fix_observation_evidence_error"] = detail.to_string().into();
    }
    if let Some(kill) = kill {
        json["kill_trigger"] = serde_json::to_value(kill.trigger)?;
        json["kill_to_cancel_ms"] = kill.kill_to_cancel_ms.into();
        json["kill_cancelled_count"] = (kill.cancellation.0 as u64).into();
        json["kill_unresolved_ids"] = serde_json::to_value(&kill.cancellation.1)?;
        json["unresolved_cancellation_ids"] = serde_json::to_value(&kill.cancellation.1)?;
    } else {
        json["kill_trigger"] = serde_json::Value::Null;
        json["kill_to_cancel_ms"] = serde_json::Value::Null;
        json["kill_cancelled_count"] = 0u64.into();
        json["kill_unresolved_ids"] = serde_json::json!([]);
        json["unresolved_cancellation_ids"] = serde_json::json!([]);
    }
    let book_healthy = json["book_tape_healthy"]
        .as_bool()
        .unwrap_or(run_plan.is_none());
    let (required_evidence_healthy, runtime_success) = terminal_runtime_health(
        &exit.reason,
        run_plan.is_some(),
        book_healthy,
        order_healthy,
        audio_healthy,
        fix_expected,
        fix_healthy,
        kill.map(|event| event.cancellation.1.is_empty()),
    );
    // A coordinated STOP/SIGTERM is a successful terminal state only after
    // cancellation is resolved and every recorder required by the immutable
    // live RunPlan has returned a healthy terminal proof.
    json["runtime_success"] = runtime_success.into();
    json["required_evidence_healthy"] = required_evidence_healthy.into();
    // This bit is set only here, after every source/executor/recorder finalizer
    // has returned and the report object contains their terminal accounting.
    json["finalized"] = true.into();
    earshot::fsio::write_atomic(report_path, serde_json::to_string_pretty(&json)?.as_bytes())?;
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
            return Err(format!(
                "{ticker} is a Mentions market — pass --allow-mention for the no-fill drill"
            )
            .into());
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
        tracing::info!(
            ticker,
            ?yes_ask,
            "mention probe: price below ask — guaranteed no fill"
        );
    }
    let status = market["market"]["status"].as_str().unwrap_or("?");
    if status != "active" {
        return Err(format!("{ticker} status is {status:?}, not active").into());
    }
    tracing::info!(ticker, category, "probe target validated (active)");

    let gate = RiskGate::new(
        RiskConfig {
            allowlist_tickers: vec![ticker.to_string()],
            ..Default::default()
        },
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
        "latency: build+sign {} µs, send→hdrs {} µs, hdrs→ack {} µs, send→ack {} µs  [{:?}]",
        send_ts.saturating_since(t0) / 1000,
        ack.hdrs_ts.saturating_since(send_ts) / 1000,
        ack.ack_ts.saturating_since(ack.hdrs_ts) / 1000,
        ack.ack_ts.saturating_since(send_ts) / 1000,
        ack.http_version,
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
    let watcher = spawn_kill_watcher(
        gate.clone(),
        stop_file.clone(),
        move || {
            let rest = rest_for_kill.clone();
            let ids = ids.clone();
            let t0 = t0_w.clone();
            async move {
                *t0.lock().unwrap() = Some(MonotonicNs::now());
                let n = rest.cancel_orders(&ids).await;
                tracing::info!(
                    cancelled = n,
                    of = ids.len(),
                    "drill: session orders cancelled"
                );
            }
        },
        || {},
    );

    if auto {
        tokio::time::sleep(Duration::from_secs(1)).await;
        std::fs::write(&stop_file, "kill drill")?;
        println!("STOP file created");
    } else {
        println!("touch {} to trigger the kill switch…", stop_file.display());
    }

    watcher.await?;
    let elapsed = t0
        .lock()
        .unwrap()
        .map(|t| MonotonicNs::now().saturating_since(t));
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
        Some(ms) if ms < 1000 => {
            println!("kill drill PASSED (session order cancelled in {ms} ms, others untouched)")
        }
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
                "t+{i:>3}s  bid {:?}  ask {:?}  (qty=centi-contracts)  levels {}/{}  seq {}  feed_age {} ms",
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

#[cfg(test)]
mod assembly_tests {
    use super::*;
    use std::sync::{Mutex as StdMutex, MutexGuard as StdMutexGuard};

    static ENV_LOCK: StdMutex<()> = StdMutex::new(());

    struct EnvRestore<'a> {
        _lock: StdMutexGuard<'a, ()>,
        values: Vec<(&'static str, Option<std::ffi::OsString>)>,
    }

    impl EnvRestore<'static> {
        fn set(values: &[(&'static str, &std::ffi::OsStr)]) -> Self {
            let lock = ENV_LOCK
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let previous = values
                .iter()
                .map(|(key, _)| (*key, std::env::var_os(key)))
                .collect();
            for (key, value) in values {
                // SAFETY: assembly tests that mutate these process variables
                // use one combined guard and do not spawn threads.
                unsafe { std::env::set_var(key, value) };
            }
            Self {
                _lock: lock,
                values: previous,
            }
        }
    }

    impl Drop for EnvRestore<'_> {
        fn drop(&mut self) {
            for (key, value) in self.values.drain(..) {
                // SAFETY: see EnvRestore::set.
                unsafe {
                    match value {
                        Some(value) => std::env::set_var(key, value),
                        None => std::env::remove_var(key),
                    }
                }
            }
        }
    }

    fn admission_config(source: &str) -> config::EventConfig {
        toml::from_str(&format!(
            r#"
[event]
id = "KX-ADMISSION-TEST"
starts_at = "2026-08-11T00:00:00Z"

[kalshi]
env = "demo"
series = "KXADMISSION"

{source}

[[keywords]]
market_ticker = "KX-ADMISSION-TEST-WORD"
phrase = "word"
variants = ["word"]
max_price_cents = 1
budget_contracts = 1
policy = "confirmed_only"
"#
        ))
        .unwrap()
    }

    #[test]
    fn default_arm_paths_are_event_scoped() {
        let (state, report) = resolve_arm_paths("KX-EVENT-PHONE", None, None);
        assert_eq!(state, PathBuf::from("state/KX-EVENT-PHONE"));
        assert_eq!(report, PathBuf::from("state/KX-EVENT-PHONE/report.json"));

        let explicit = PathBuf::from("/tmp/earshot-test/event-lane");
        let (state, report) = resolve_arm_paths("IGNORED", Some(explicit.clone()), None);
        assert_eq!(state, explicit);
        assert_eq!(report, state.join("report.json"));
    }

    #[test]
    fn live_state_root_refuses_an_alternate_parent() {
        let tmp = tempfile::tempdir().unwrap();
        let canonical = tmp.path().join("canonical");
        let alternate = tmp.path().join("alternate");
        std::fs::create_dir_all(&canonical).unwrap();
        std::fs::create_dir_all(&alternate).unwrap();
        let state = alternate.join("KX-EVENT-PHONE");
        std::fs::create_dir_all(&state).unwrap();
        let error = enforce_live_state_root(
            Path::new("unused-when-root-is-pinned"),
            &state,
            Some(&canonical),
        )
        .unwrap_err();
        assert!(error.to_string().contains("outside canonical root"));
    }

    #[test]
    fn live_state_root_accepts_only_a_direct_event_child() {
        let tmp = tempfile::tempdir().unwrap();
        let canonical = tmp.path().join("state");
        let state = canonical.join("KX-EVENT-WEBCAST");
        std::fs::create_dir_all(&state).unwrap();
        let resolved = enforce_live_state_root(
            Path::new("unused-when-root-is-pinned"),
            &state,
            Some(&canonical),
        )
        .unwrap();
        assert_eq!(resolved, std::fs::canonicalize(canonical).unwrap());
    }

    #[test]
    fn readiness_cleanup_preserves_live_owner_and_removes_dead_owner() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(LISTENER_READY_FILE);
        std::fs::write(
            &path,
            format!("{{\"port\":8080,\"pid\":{}}}\n", std::process::id()),
        )
        .unwrap();
        assert!(clear_stale_listener_ready(dir.path()).is_err());
        assert!(path.exists(), "live arm's readiness token was deleted");

        std::fs::write(&path, "{\"port\":8080,\"pid\":4294967295}\n").unwrap();
        clear_stale_listener_ready(dir.path()).unwrap();
        assert!(
            !path.exists(),
            "dead-owner readiness token survived cleanup"
        );
    }

    #[test]
    fn readiness_cleanup_removes_malformed_stale_token() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(LISTENER_READY_FILE);
        std::fs::write(&path, "not-json\n").unwrap();
        clear_stale_listener_ready(dir.path()).unwrap();
        assert!(!path.exists());
    }

    #[test]
    fn startup_removes_stale_stream_connected_after_ownership_check() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(STREAM_CONNECTED_FILE);
        std::fs::write(&path, "{\"call_sid\":\"old\"}\n").unwrap();
        clear_stale_stream_connected(dir.path()).unwrap();
        assert!(!path.exists());
    }

    #[test]
    fn phone_admission_requires_fresh_nonce_echo() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = admission_config(
            r#"[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:0""#,
        );
        std::fs::write(
            dir.path().join(ADMISSION_RECEIPT_FILE),
            r#"{"schema_version":1,"event_id":"KX-ADMISSION-TEST","nonce":"stale","status":"admitted"}"#,
        )
        .unwrap();

        let admission = create_admission_interlock(&cfg, dir.path(), true).unwrap();
        assert!(
            !admission.is_open(),
            "stale prior-run receipt reopened trading"
        );
        let challenge: serde_json::Value = serde_json::from_slice(
            &std::fs::read(dir.path().join(ADMISSION_CHALLENGE_FILE)).unwrap(),
        )
        .unwrap();
        std::fs::write(
            dir.path().join(STREAM_CONNECTED_FILE),
            r#"{"unix_ts":1,"call_sid":"CA-fresh","attempt":0}"#,
        )
        .unwrap();
        let receipt = serde_json::json!({
            "schema_version": 1,
            "event_id": cfg.event.id,
            "nonce": challenge["nonce"],
            "status": "admitted",
            "call_sid": "CA-fresh",
            "stream_attempt": 0,
        });
        earshot::fsio::write_atomic(
            &dir.path().join(ADMISSION_RECEIPT_FILE),
            serde_json::to_vec(&receipt).unwrap().as_slice(),
        )
        .unwrap();
        assert!(
            admission.is_open(),
            "matching durable admission receipt stayed closed"
        );

        std::fs::write(
            dir.path().join(STREAM_CONNECTED_FILE),
            r#"{"unix_ts":2,"call_sid":"CA-redial","attempt":1}"#,
        )
        .unwrap();
        assert!(
            !admission.is_open(),
            "a redial reused the prior stream's admission receipt"
        );
    }

    #[test]
    fn non_phone_and_rehearsal_pipelines_are_open_without_receipts() {
        let dir = tempfile::tempdir().unwrap();
        let replay = admission_config(
            r#"[[sources]]
kind = "replay"
path = "fixture.wav""#,
        );
        assert!(create_admission_interlock(&replay, dir.path(), true)
            .unwrap()
            .is_open());
        let phone = admission_config(
            r#"[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:0""#,
        );
        assert!(create_admission_interlock(&phone, dir.path(), false)
            .unwrap()
            .is_open());
    }

    #[test]
    fn local_account_identity_uses_the_same_config_first_selector_as_arm() {
        use rsa::pkcs1::EncodeRsaPrivateKey;
        use rsa::RsaPrivateKey;

        let dir = tempfile::tempdir().unwrap();
        let env_key = RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 1024).unwrap();
        let env_pem = env_key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
        let env_path = dir.path().join("ambient.pem");
        std::fs::write(&env_path, env_pem.as_bytes()).unwrap();
        let _env = EnvRestore::set(&[
            ("KALSHI_API_KEY_ID", std::ffi::OsStr::new("ambient-id")),
            ("KALSHI_PRIVATE_KEY_PATH", env_path.as_os_str()),
        ]);

        let config_path = dir.path().join("event.toml");
        let mut ambient_cfg = admission_config(
            r#"[[sources]]
kind = "replay"
path = "fixture.wav""#,
        );
        let ambient = load_signer_for(&ambient_cfg, &config_path).unwrap();
        let expected_ambient = Signer::from_pem("ambient-id".into(), &env_pem).unwrap();
        assert_eq!(
            ambient.account_fingerprint(),
            expected_ambient.account_fingerprint()
        );

        let bound_key = RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 1024).unwrap();
        let bound_pem = bound_key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
        std::fs::write(dir.path().join("bound.pem"), bound_pem.as_bytes()).unwrap();
        ambient_cfg.kalshi.key_id = Some("bound-id".to_string());
        ambient_cfg.kalshi.private_key_path = Some("bound.pem".to_string());
        let selected = load_signer_for(&ambient_cfg, &config_path).unwrap();
        let expected_bound = Signer::from_pem("bound-id".into(), &bound_pem).unwrap();
        assert_eq!(
            selected.account_fingerprint(),
            expected_bound.account_fingerprint(),
            "config-bound credentials must win over ambient profile credentials"
        );
        assert_ne!(
            selected.account_fingerprint(),
            expected_ambient.account_fingerprint()
        );
    }

    #[tokio::test]
    async fn book_feed_shutdown_drops_producer_before_joining_writer() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("book.jsonl");
        let tickers = vec!["KX-BOOK-TEST".to_string()];
        let cache = BookCache::new(&tickers);
        let (tap, tape) = BookTapeTask::start_new(
            &path,
            BookTapeSession {
                endpoint: "wss://example.invalid/trade-api/ws/v2".to_string(),
                event: Some("KX-BOOK".to_string()),
                label: "test-run".to_string(),
                tickers,
                host: "test-host".to_string(),
                git_sha: "test-sha".to_string(),
                clock_offset_s: Some(0.0),
                planned_secs: None,
            },
            cache,
            8,
            BookTapeOptions::default(),
        )
        .await
        .unwrap();
        let ws = tokio::spawn(async move {
            let _tap = tap;
            std::future::pending::<()>().await;
        });

        let (summary, fault) = finalize_book_feed(ws, Some(tape), true).await;
        let summary = summary.expect("writer returned terminal accounting");
        assert!(summary.producer_quiesced);
        assert!(summary.terminal_accounting_complete);
        assert!(fault
            .as_deref()
            .is_some_and(|detail| detail.contains("book tape incomplete")));
    }

    #[test]
    fn graceful_kill_requires_resolved_cancellation_and_all_live_evidence() {
        let (required, success) = terminal_runtime_health(
            &ExitReason::Sigterm,
            true,
            true,
            true,
            true,
            true,
            true,
            Some(true),
        );
        assert!(required);
        assert!(success);

        let (required, success) = terminal_runtime_health(
            &ExitReason::StopFile,
            true,
            true,
            true,
            false,
            false,
            false,
            Some(true),
        );
        assert!(!required, "missing audio proof must fail a live run");
        assert!(!success);

        let (required, success) = terminal_runtime_health(
            &ExitReason::Sigterm,
            true,
            true,
            true,
            true,
            false,
            false,
            Some(false),
        );
        assert!(required, "REST does not require a FIX receive tape");
        assert!(
            !success,
            "unresolved cancellation must fail a graceful kill"
        );
    }

    #[test]
    fn fix_health_is_null_for_rest_and_boolean_for_fix() {
        assert_eq!(
            nullable_required_health(false, false),
            serde_json::Value::Null
        );
        assert_eq!(
            nullable_required_health(true, false),
            serde_json::Value::Bool(false)
        );
        assert_eq!(
            nullable_required_health(true, true),
            serde_json::Value::Bool(true)
        );
    }

    #[test]
    fn schema_three_run_plan_hash_identity_and_exact_artifacts_are_verified() {
        use rsa::sha2::{Digest, Sha256};
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let config_dir = repo.join("configs");
        let run_dir = repo.join("var/runs/KX-ADMISSION-TEST/20260811T000000Z-abcdef012345");
        std::fs::create_dir_all(&config_dir).unwrap();
        std::fs::create_dir_all(run_dir.join("control")).unwrap();
        std::fs::create_dir_all(run_dir.join("capture")).unwrap();
        std::fs::create_dir_all(run_dir.join("book")).unwrap();
        std::fs::create_dir_all(run_dir.join("orders")).unwrap();
        std::fs::create_dir_all(run_dir.join("report")).unwrap();
        std::fs::create_dir_all(run_dir.join("grade")).unwrap();
        std::fs::create_dir_all(repo.join("state")).unwrap();
        let config_path = config_dir.join("event.toml");
        let config_bytes = br#"
[event]
id = "KX-ADMISSION-TEST"
starts_at = "2026-08-11T00:00:00Z"

[kalshi]
env = "demo"
series = "KXADMISSION"

[[sources]]
kind = "pipe"
cmd = ["true"]
sample_rate_hz = 16000

[[keywords]]
market_ticker = "KX-ADMISSION-TEST-WORD"
phrase = "word"
variants = ["word"]
max_price_cents = 1
budget_contracts = 1
policy = "confirmed_only"
"#;
        std::fs::write(&config_path, config_bytes).unwrap();
        let cfg = config::load(&config_path).unwrap();
        let fingerprint = format!("sha256:{}", "a".repeat(64));
        let mut plan = serde_json::json!({
            "schema": 3,
            "kind": "run-plan",
            "event_id": cfg.event.id,
            "run_id": "20260811T000000Z-abcdef012345",
            "created_utc": "2026-08-10T00:00:00Z",
            "starts_at_utc": cfg.event.starts_at,
            "lane": "webcast",
            "execution_enabled": true,
            "kalshi_env": "demo",
            "market_event_ticker": "KX-ADMISSION-TEST",
            "execution_host_id": "test-host",
            "capture_only": false,
            "config_path": "configs/event.toml",
            "config_sha256": format!("{:x}", Sha256::digest(config_bytes)),
            "event_env_path": null,
            "event_env_sha256": null,
            "required_prep_receipts": ["prepared", "access_acquired", "study_completed"],
            "artifacts": {
                "receipts": "control/receipts.jsonl",
                "status": "control/status.json",
                "audio_raw": "capture/audio.raw",
                "audio_frames": "capture/audio.frames.jsonl",
                "audio_manifest": "capture/audio.manifest.json",
                "audio_commit": "capture/audio.commit.json",
                "audio_lock": "capture/audio.lock",
                "audio_wav_dir": "capture/wav",
                "book_tape": "book/kalshi-tape.jsonl",
                "order_evidence": "orders/order-evidence.jsonl",
                "fix_observation_tape": "orders/fix-observations.jsonl",
                "runtime_report": "report/report.json",
                "grade": "grade/verdict.json"
            },
            "account_fingerprint": fingerprint,
            "canonical_state_root": "state"
        });
        plan["plan_sha256"] =
            serde_json::Value::String(canonical_plan_sha256(plan.clone()).unwrap());
        let plan_path = run_dir.join("control/run-plan.json");
        std::fs::write(&plan_path, serde_json::to_vec_pretty(&plan).unwrap()).unwrap();
        let transitions = [
            "plan_compiled",
            "prepared",
            "access_acquired",
            "study_completed",
            "preflight_passed",
            "scheduled",
            "capture_ready",
        ];
        let mut previous = plan["plan_sha256"].as_str().unwrap().to_owned();
        let mut receipts = Vec::new();
        for (index, transition) in transitions.into_iter().enumerate() {
            let mut receipt = serde_json::json!({
                "schema": 1,
                "kind": "transition",
                "event": cfg.event.id,
                "run": "20260811T000000Z-abcdef012345",
                "sequence": index as u64 + 1,
                "previous_sha256": previous,
                "transition": transition,
                "outcome": "ok",
            });
            let claimed = canonical_plan_sha256(receipt.clone()).unwrap();
            receipt["receipt_sha256"] = claimed.clone().into();
            let mut line = Vec::new();
            canonical_json(&receipt, &mut line).unwrap();
            receipts.extend_from_slice(&line);
            receipts.push(b'\n');
            previous = claimed;
        }
        std::fs::write(run_dir.join("control/receipts.jsonl"), receipts).unwrap();
        let _env = EnvRestore::set(&[
            ("EARSHOT_REPO", repo.as_os_str()),
            ("EARSHOT_RUN_PLAN", plan_path.as_os_str()),
            ("EARSHOT_EXECUTION_HOST", std::ffi::OsStr::new("test-host")),
            (
                "EARSHOT_KALSHI_ACCOUNT_FINGERPRINT",
                std::ffi::OsStr::new(fingerprint.as_str()),
            ),
        ]);
        let state_root = std::fs::canonicalize(repo.join("state")).unwrap();
        let verified =
            verify_runtime_run_plan(&config_path, &cfg, &KalshiEnv::demo(), &state_root).unwrap();
        let canonical_run_dir = std::fs::canonicalize(&run_dir).unwrap();
        assert_eq!(verified.run_dir, canonical_run_dir);
        assert_eq!(
            verified.book_tape_path,
            canonical_run_dir.join("book/kalshi-tape.jsonl")
        );
        assert_eq!(
            verified.order_evidence_path,
            canonical_run_dir.join("orders/order-evidence.jsonl")
        );
        assert_eq!(
            verified.audio_raw_path,
            canonical_run_dir.join("capture/audio.raw")
        );
        assert_eq!(
            verified.fix_observation_tape_path,
            canonical_run_dir.join("orders/fix-observations.jsonl")
        );

        let valid_plan = plan.clone();
        plan["artifacts"]["unexpected"] = "report/unexpected.json".into();
        plan["plan_sha256"] =
            serde_json::Value::String(canonical_plan_sha256(plan.clone()).unwrap());
        std::fs::write(&plan_path, serde_json::to_vec_pretty(&plan).unwrap()).unwrap();
        assert!(
            verify_runtime_run_plan(&config_path, &cfg, &KalshiEnv::demo(), &state_root,)
                .unwrap_err()
                .to_string()
                .contains("must exactly match schema 3")
        );

        plan = valid_plan;
        plan["execution_host_id"] = serde_json::Value::String("other-host".into());
        plan["plan_sha256"] =
            serde_json::Value::String(canonical_plan_sha256(plan.clone()).unwrap());
        std::fs::write(&plan_path, serde_json::to_vec_pretty(&plan).unwrap()).unwrap();
        assert!(
            verify_runtime_run_plan(&config_path, &cfg, &KalshiEnv::demo(), &state_root,)
                .unwrap_err()
                .to_string()
                .contains("execution host")
        );
    }
}
