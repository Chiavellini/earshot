//! Speed budgets for every hot-path artifact. All #[ignore]d — run with:
//!   cargo test --release -- --ignored perf_
//! Budgets are 2–3× the measured values on the dev laptop (2026-07-11) to
//! stay flake-free; every test PRINTS its measurement so a green run still
//! yields the numbers. Debug builds are 10–50× slower — release only.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use earshot::clock::MonotonicNs;
use earshot::config::{KeywordConfig, Policy};
use earshot::decision::DecisionTable;
use earshot::detector::sherpa_kws::SherpaKwsDetector;
use earshot::detector::stub::EnergyStubDetector;
use earshot::detector::Detector;
use earshot::fixture::{Burst, FixtureSpec};
use earshot::kalshi::auth::{Presigner, Signer};
use earshot::kalshi::book::{Book, BookCache};
use earshot::kalshi::executor::{prestage_orders, run_executor, ExecCtx};
use earshot::kalshi::rest::RestClient;
use earshot::pipeline::preproc::{HalfBandUpsampler2x, PreprocCfg};
use earshot::pipeline::run_pipeline;
use earshot::risk::{RiskConfig, RiskGate};
use earshot::source::replay::ReplaySource;
use earshot::telemetry::LatencyRecorder;
use earshot::types::{AudioChunk, DecisionIntent, KeywordId, SourceId, Tier};
use rsa::pkcs1::EncodeRsaPrivateKey;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const MODEL_DIR: &str = "models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01";
const TICKER: &str = "KX-PERF";

fn test_signer() -> Arc<Signer> {
    let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
    let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
    Arc::new(Signer::from_pem("perf-key".into(), &pem).unwrap())
}

fn percentile(sorted: &[u64], q: f64) -> u64 {
    sorted[((sorted.len() as f64 - 1.0) * q) as usize]
}

/// Preproc resampler: must be a rounding error next to everything else.
#[test]
#[ignore]
fn perf_preproc_rtf() {
    let mut up = HalfBandUpsampler2x::new();
    // 60 s of 8 kHz audio in 20 ms frames, as the pipeline delivers it.
    let frame: Vec<i16> = (0..160)
        .map(|i| (10_000.0 * (2.0 * std::f32::consts::PI * 440.0 * i as f32 / 8000.0).sin()) as i16)
        .collect();
    let frames = 60 * 50;
    let t0 = Instant::now();
    let mut sink = 0i64;
    for _ in 0..frames {
        let out = up.process(&frame);
        sink += out[0] as i64; // keep the optimizer honest
    }
    let elapsed = t0.elapsed().as_secs_f64();
    let rtf = 60.0 / elapsed;
    println!("preproc 2x upsampler: 60 s audio in {elapsed:.4} s → {rtf:.0}× realtime (sink {sink})");
    assert!(rtf > 100.0, "resampler only {rtf:.0}× realtime");
}

/// Detector: the whole strategy dies if sherpa can't keep up with live audio.
/// RTF and per-chunk latency both matter: a chunk must decode in well under
/// one chunk-duration or the pipeline falls progressively behind.
#[test]
#[ignore]
fn perf_detector_rtf() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join(MODEL_DIR);
    if !dir.exists() {
        if std::env::var_os("EARSHOT_REQUIRE_MODELS").is_some() {
            panic!("EARSHOT_REQUIRE_MODELS set but {MODEL_DIR} missing");
        }
        eprintln!("SKIP perf_detector_rtf: models missing");
        return;
    }
    let kw: KeywordConfig = toml::from_str(
        r#"
        market_ticker    = "KX-PERF"
        phrase           = "tariff"
        variants         = ["tariff", "tariffs"]
        max_price_cents  = 97
        budget_contracts = 200
        policy           = "two_tier"
        fast_prefixes    = ["tari"]
        "#,
    )
    .unwrap();
    let sherpa_cfg = toml::from_str(&format!("model_dir = {:?}", dir.display().to_string())).unwrap();
    let mut det = SherpaKwsDetector::new(&sherpa_cfg, std::slice::from_ref(&kw)).unwrap();

    // 60 s of real speech: tile the model's own test WAV (16 kHz).
    let mut reader = hound::WavReader::open(dir.join("test_wavs/0.wav")).unwrap();
    assert_eq!(reader.spec().sample_rate, 16_000);
    let base: Vec<i16> = reader.samples::<i16>().map(|s| s.unwrap()).collect();
    let target = 16_000 * 60;
    let pcm: Vec<i16> = base.iter().copied().cycle().take(target).collect();

    let chunk_samples = 16_000 * 160 / 1000;
    let mut per_chunk_us: Vec<u64> = Vec::with_capacity(pcm.len() / chunk_samples + 1);
    let t0 = Instant::now();
    for (seq, window) in pcm.chunks(chunk_samples).enumerate() {
        let now = MonotonicNs::now();
        let chunk = AudioChunk {
            pcm: Arc::from(window),
            sample_rate_hz: 16_000,
            first_capture_ts: now,
            last_capture_ts: now,
            first_preproc_ts: now,
            chunk_ts: now,
            source: SourceId(0),
            seq: seq as u64,
        };
        let c0 = Instant::now();
        let _ = det.process(&chunk);
        per_chunk_us.push(c0.elapsed().as_micros() as u64);
    }
    let elapsed = t0.elapsed().as_secs_f64();
    let rtf = 60.0 / elapsed;
    per_chunk_us.sort_unstable();
    let p50 = percentile(&per_chunk_us, 0.50);
    let p95 = percentile(&per_chunk_us, 0.95);
    let max = *per_chunk_us.last().unwrap();
    println!(
        "sherpa_kws: 60 s speech in {elapsed:.2} s → {rtf:.1}× realtime; per-160ms-chunk \
         p50 {p50} µs  p95 {p95} µs  max {max} µs"
    );
    assert!(rtf > 3.0, "detector only {rtf:.1}× realtime — cannot keep up live");
    assert!(p95 < 80_000, "p95 per-chunk {p95} µs ≥ half a chunk — pipeline would drift");
}

/// Executor fire path: from intent to wire must be microseconds — the
/// network is allowed to be slow, our machinery is not.
#[tokio::test]
#[ignore]
async fn perf_executor_fire_path() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(ResponseTemplate::new(201).set_body_json(
            serde_json::json!({"order": {"order_id": "srv-p", "status": "canceled"}}),
        ))
        .mount(&server)
        .await;

    let signer = test_signer();
    let rest =
        RestClient::with_base_and_timeout(server.uri(), signer.clone(), Duration::from_secs(5))
            .unwrap();
    let cache = BookCache::new(&[TICKER.to_string()]);
    cache.touch();
    cache.publish(
        TICKER,
        Book { yes_bids: vec![(5, 100)], yes_asks: vec![(10, 500)], seq: 1, as_of: MonotonicNs::now() },
    );
    let state = tempfile::tempdir().unwrap();
    let gate = Arc::new(
        RiskGate::new(
            RiskConfig { allowlist_tickers: vec![TICKER.into()], ..Default::default() },
            state.path(),
        )
        .unwrap(),
    );
    gate.arm();
    let presigner = Presigner::new(signer, "POST", "/trade-api/v2/portfolio/events/orders".into());

    // Presigner get(): wait-free header load on the fire path.
    let t0 = Instant::now();
    let mut sink = 0usize;
    for _ in 0..10_000 {
        let (headers, _age) = presigner.get();
        sink += headers[0].1.len();
    }
    let per_get_ns = t0.elapsed().as_nanos() as u64 / 10_000;
    println!("presigner.get(): {per_get_ns} ns/call (sink {sink})");
    assert!(per_get_ns < 50_000, "presigner get {per_get_ns} ns — not wait-free?");

    // 6 keywords × 15¢ = 90¢: the deepest burst the $1 probe ledger allows.
    let kws: Vec<KeywordConfig> = (0..6)
        .map(|_| {
            toml::from_str(
                r#"
                market_ticker    = "KX-PERF"
                phrase           = "perf"
                variants         = ["perf"]
                max_price_cents  = 15
                budget_contracts = 1
                policy           = "confirmed_only"
                "#,
            )
            .unwrap()
        })
        .collect();
    let ctx = Arc::new(ExecCtx {
        rest,
        presigner,
        cache,
        gate,
        prestaged: prestage_orders("PERF", &kws),
        haircuts: (0..6u16).map(|i| (i, 0u32)).collect::<HashMap<_, _>>(),
        dry_run: false,
        registry: earshot::kalshi::OrderRegistry::default(),
    });
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    for kw in 0..6u16 {
        let now = MonotonicNs::now();
        tx.send(DecisionIntent {
            keyword: KeywordId(kw),
            market_ticker: TICKER.into(),
            max_price_cents: 15,
            tranche_contracts: 1,
            tier: Tier::Confirmed,
            audio_ts: now,
            decision_ts: now,
        })
        .await
        .unwrap();
    }
    drop(tx);
    let report = run_executor(rx, ctx).await;
    let edges = report.recorder.snapshot();
    let d2e = &edges["decision→exec"];
    let e2s = &edges["exec→send"];
    println!(
        "executor: decision→exec p95 {} µs, exec→send p95 {} µs over {} live sends",
        d2e.p95_us, e2s.p95_us, e2s.count
    );
    assert_eq!(report.orders.iter().filter(|o| o.send_ts.is_some()).count(), 6);
    assert!(e2s.p95_us < 5_000, "exec→send p95 {} µs — pre-staging broken?", e2s.p95_us);
    // decision→exec here includes queue wait: this rig pre-fills the channel
    // before the executor starts, so the edge is test-shaped. Bound it only
    // loosely; the live number comes from arm's report.
    assert!(d2e.p95_us < 50_000, "decision→exec p95 {} µs — executor startup pathological", d2e.p95_us);
}

/// The headline: paced audio in → acked order out, full chain, local mock.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore]
async fn perf_full_chain_paced() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(ResponseTemplate::new(201).set_body_json(
            serde_json::json!({"order": {"order_id": "srv-fc", "status": "canceled"}}),
        ))
        .expect(1)
        .mount(&server)
        .await;

    let signer = test_signer();
    let rest =
        RestClient::with_base_and_timeout(server.uri(), signer.clone(), Duration::from_secs(5))
            .unwrap();
    let cache = BookCache::new(&[TICKER.to_string()]);
    cache.touch();
    cache.publish(
        TICKER,
        Book { yes_bids: vec![(40, 100)], yes_asks: vec![(85, 500)], seq: 1, as_of: MonotonicNs::now() },
    );
    let state = tempfile::tempdir().unwrap();
    let gate = Arc::new(
        RiskGate::new(
            RiskConfig { allowlist_tickers: vec![TICKER.into()], ..Default::default() },
            state.path(),
        )
        .unwrap(),
    );
    gate.arm();
    let keywords = vec![KeywordConfig {
        market_ticker: TICKER.into(),
        phrase: "tone".into(),
        variants: vec!["tone".into()],
        max_price_cents: 90,
        budget_contracts: 1,
        policy: Policy::FastOnly,
        rule_risk_haircut_cents: 0,
        fast_prefixes: vec![],
        fast_threshold: None,
        confirmed_threshold: None,
        boost: None,
        min_confidence_fast: 0.0,
        min_confidence_confirmed: 0.0,
    }];
    let presigner = Presigner::new(signer, "POST", "/trade-api/v2/portfolio/events/orders".into());
    let ctx = Arc::new(ExecCtx {
        rest,
        presigner,
        cache,
        gate,
        prestaged: prestage_orders("PERF", &keywords),
        haircuts: HashMap::from([(0u16, 0u32)]),
        dry_run: false,
        registry: earshot::kalshi::OrderRegistry::default(),
    });

    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("f.wav");
    FixtureSpec {
        sample_rate_hz: 8000,
        total_ms: 2000,
        tone_hz: 440.0,
        amplitude: 0.6,
        bursts: vec![Burst { onset_ms: 1000, dur_ms: 500 }],
    }
    .write_wav(&wav)
    .unwrap();
    let source = Box::new(ReplaySource::from_wav(&wav, 1.0, SourceId(0)).unwrap());
    let detector = Box::new(EnergyStubDetector::for_standard_fixture());
    let table = DecisionTable::from_keywords(&keywords);

    let (intent_tx, intent_rx) = tokio::sync::mpsc::channel(64);
    let exec_task = tokio::spawn(run_executor(intent_rx, ctx));
    let mut report = tokio::task::spawn_blocking(move || {
        run_pipeline("PERF", 160, PreprocCfg::default(), source, detector, table, Some(intent_tx))
    })
    .await
    .unwrap()
    .unwrap();
    report.edges.extend(exec_task.await.unwrap().recorder.snapshot());

    let c2d = &report.edges["capture→decision"];
    let c2a = &report.edges["capture→ack"];
    println!(
        "full chain paced: capture→decision p95 {} µs, capture→ack p95 {} µs",
        c2d.p95_us, c2a.p95_us
    );
    assert!(c2d.p95_us < 170_000, "capture→decision p95 {} µs > chunk + 10 ms", c2d.p95_us);
    assert!(c2a.p95_us < 200_000, "capture→ack p95 {} µs vs local mock", c2a.p95_us);
}

/// Recorder overhead: it sits on the detect thread's inner loop.
#[test]
#[ignore]
fn perf_recorder_overhead() {
    let mut r = LatencyRecorder::new();
    let t0 = Instant::now();
    for i in 0..1_000_000u64 {
        r.record("chunk→detect", i % 1_000_000);
    }
    let per_op_ns = t0.elapsed().as_nanos() as u64 / 1_000_000;
    println!("LatencyRecorder.record(): {per_op_ns} ns/op");
    assert!(per_op_ns < 100, "recorder {per_op_ns} ns/op — too hot for the detect loop");
}
