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
use earshot::kalshi::transport::{OrderTransport, StageWire};
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
///
/// Measures the WS-2 chunk-size ladder (160 / 80 prod / 40 / 20 candidates):
/// chunk_ms is a pure latency dial for sherpa_kws (BAKEOFF.md — the model
/// re-buffers internally, accuracy is bit-identical), so the only question is
/// whether the per-call overhead at smaller chunks keeps p95 under half a
/// chunk-duration. Run this ON THE EVENT BOX before shipping a smaller
/// chunk_ms — laptop numbers guide, the box decides.
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

    // 60 s of real speech: tile the model's own test WAV (16 kHz).
    let mut reader = hound::WavReader::open(dir.join("test_wavs/0.wav")).unwrap();
    assert_eq!(reader.spec().sample_rate, 16_000);
    let base: Vec<i16> = reader.samples::<i16>().map(|s| s.unwrap()).collect();
    let target = 16_000 * 60;
    let pcm: Vec<i16> = base.iter().copied().cycle().take(target).collect();

    // (chunk_ms, hard p95 budget in µs, or None = report-only).
    // 160 keeps the original bar (p95 < half a chunk). Below the model's
    // internal decode quantum (chunk-16 export) per-call time turns BIMODAL —
    // most calls are near-zero, every Nth absorbs the full decode burst
    // (measured 2026-07-17: chunk 80 p50 345 µs / p95 71 ms / max 197 ms) —
    // so "p95 < chunk/2" is the wrong stability rule there; stability is
    // throughput (the RTF > 3 assert) and the burst tail is structural to
    // sherpa (the streaming_ctc milestone's territory, not a chunk_ms
    // property). 80/40/20 are report-only: the numbers feed the on-box WS-2
    // decision, and a noisy dev laptop can't green/red-light a prod change.
    let ladder: [(u32, Option<u64>); 4] =
        [(160, Some(80_000)), (80, None), (40, None), (20, None)];

    // Measure and PRINT the whole ladder first, assert only afterwards: a
    // budget miss at one size must not hide the other sizes' numbers — the
    // printout IS the deliverable of this test (the asserts are drift alarms).
    let mut results = Vec::new();
    for (chunk_ms, p95_budget_us) in ladder {
        // Fresh detector per size: the two OnlineStreams carry state, and the
        // sizes must not contaminate each other's timing.
        let sherpa_cfg =
            toml::from_str(&format!("model_dir = {:?}", dir.display().to_string())).unwrap();
        let mut det = SherpaKwsDetector::new(&sherpa_cfg, std::slice::from_ref(&kw)).unwrap();

        let chunk_samples = 16_000 * chunk_ms as usize / 1000;
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
            "sherpa_kws chunk {chunk_ms} ms: 60 s speech in {elapsed:.2} s → {rtf:.1}× realtime; \
             per-chunk p50 {p50} µs  p95 {p95} µs  max {max} µs{}",
            if p95_budget_us.is_none() { "  [candidate: report-only]" } else { "" }
        );
        results.push((chunk_ms, p95_budget_us, rtf, p95));
    }
    for (chunk_ms, p95_budget_us, rtf, p95) in results {
        assert!(rtf > 3.0, "detector only {rtf:.1}× realtime at chunk {chunk_ms} — cannot keep up live");
        if let Some(budget) = p95_budget_us {
            assert!(
                p95 < budget,
                "chunk {chunk_ms}: p95 per-chunk {p95} µs ≥ half a chunk — pipeline would drift"
            );
        }
    }
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
        transport: OrderTransport::Rest { client: rest.clone(), presigner },
        rest,
        cache,
        gate,
        prestaged: prestage_orders("PERF", &kws, StageWire::Rest),
        haircuts: (0..6u16).map(|i| (i, 0u32)).collect::<HashMap<_, _>>(),
        dry_run: false,
        registry: earshot::kalshi::OrderRegistry::default(),
    });
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    for kw in 0..6u16 {
        let now = MonotonicNs::now();
        tx.send(DecisionIntent {
            keyword: KeywordId(kw),
            source: earshot::types::SourceId(0),
            market_ticker: TICKER.into(),
            max_price_cents: 15,
            tranche_contracts: 1,
            tier: Tier::Confirmed,
            audio_ts: now,
            decision_ts: now,
            handoff_ts: now,
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
        transport: OrderTransport::Rest { client: rest.clone(), presigner },
        rest,
        cache,
        gate,
        prestaged: prestage_orders("PERF", &keywords, StageWire::Rest),
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
        run_pipeline("PERF", 160, PreprocCfg::default(), source, detector, table, Some(intent_tx), Default::default())
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

/// Cold first fire vs steady state, A/B on the arm-time shadow warm-fire.
/// The socket is warmed at arm (rest.warm()), but the first REAL fire still
/// pays cold lazy-init / first-parse costs along handle_intent →
/// create_order_presigned → JSON parse — and the first fire is the one that
/// decides the race. Four fresh rigs run alternating [warm-off, warm-on,
/// warm-off, warm-on]; the HEADLINE pair is rigs 3–4, where process-wide
/// state (allocator, lazy statics) is hot for both and the remaining delta
/// isolates the per-client warm-fire effect — exactly what arm's
/// `order_by_client_id` shadow warm buys. Caveat printed with the result:
/// wiremock/localhost proves CODE-PATH warming, not network behavior.
#[tokio::test]
#[ignore]
async fn perf_cold_first_fire() {
    async fn rig_cold_premium(warm_code_path: bool) -> (u64, u64) {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/exchange/status"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "exchange_active": true, "trading_active": true
            })))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/portfolio/orders"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "orders": []
            })))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/portfolio/events/orders"))
            .respond_with(ResponseTemplate::new(201).set_body_json(
                serde_json::json!({"order": {"order_id": "srv-c", "status": "canceled"}}),
            ))
            .mount(&server)
            .await;

        let signer = test_signer();
        let rest =
            RestClient::with_base_and_timeout(server.uri(), signer.clone(), Duration::from_secs(5))
                .unwrap();
        // Socket warm always (arm always does it) — the A/B variable is the
        // code-path warm below, not the TCP handshake.
        rest.warm().await;
        if warm_code_path {
            // The arm-time shadow warm-fire, verbatim posture.
            let _ = rest.order_by_client_id("earshot-warmfire-never-an-order").await;
        }

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
        let presigner =
            Presigner::new(signer, "POST", "/trade-api/v2/portfolio/events/orders".into());
        // 6 × 15¢ = 90¢: deepest sequence inside the $1 probe ledger.
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
            transport: OrderTransport::Rest { client: rest.clone(), presigner },
            rest,
            cache,
            gate,
            prestaged: prestage_orders("PERF", &kws, StageWire::Rest),
            haircuts: (0..6u16).map(|i| (i, 0u32)).collect::<HashMap<_, _>>(),
            dry_run: false,
            registry: earshot::kalshi::OrderRegistry::default(),
        });

        // One intent at a time with a gap, so fire #1 is genuinely first and
        // the rest are steady-state — no queue-wait pollution.
        let (tx, rx) = tokio::sync::mpsc::channel(8);
        let exec = tokio::spawn(run_executor(rx, ctx));
        for kw in 0..6u16 {
            let now = MonotonicNs::now();
            tx.send(DecisionIntent {
                keyword: KeywordId(kw),
                source: earshot::types::SourceId(0),
                market_ticker: TICKER.into(),
                max_price_cents: 15,
                tranche_contracts: 1,
                tier: Tier::Confirmed,
                audio_ts: now,
                decision_ts: now,
                handoff_ts: now,
            })
            .await
            .unwrap();
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        drop(tx);
        let report = exec.await.unwrap();

        let mut fires: Vec<(u64, u64)> = report
            .orders
            .iter()
            .filter_map(|o| Some((o.exec_ts, o.send_ts?.saturating_sub(o.exec_ts))))
            .collect();
        assert_eq!(fires.len(), 6, "all 6 fires must send");
        fires.sort_unstable_by_key(|f| f.0); // order by exec_ts: first = cold
        let cold_send_ns = fires[0].1;
        let mut steady: Vec<u64> = fires[1..].iter().map(|f| f.1).collect();
        steady.sort_unstable();
        (cold_send_ns, percentile(&steady, 0.50))
    }

    let mut rows = Vec::new();
    for (i, warm) in [false, true, false, true].into_iter().enumerate() {
        let (cold, steady) = rig_cold_premium(warm).await;
        println!(
            "rig {} warm-fire={:5}: cold exec→send {} µs, steady p50 {} µs, premium {} µs",
            i + 1, warm, cold / 1000, steady / 1000, cold.saturating_sub(steady) / 1000
        );
        rows.push((warm, cold, steady));
        // Loose ceiling (2–3× convention) applies to every rig.
        assert!(cold < 20_000_000, "cold exec→send {} µs — first-fire path pathological", cold / 1000);
    }
    let (_, off_cold, off_steady) = rows[2]; // process-warm pair
    let (_, on_cold, on_steady) = rows[3];
    println!(
        "HEADLINE (process-warm pair): warm-fire OFF premium {} µs vs ON premium {} µs \
         [wiremock localhost — proves code-path warming, not network]",
        off_cold.saturating_sub(off_steady) / 1000,
        on_cold.saturating_sub(on_steady) / 1000,
    );
}

/// Ring idle-wake A/B: OLD pre-unpark ring (spin 64 + bare park_timeout(100 µs),
/// vendored verbatim below from 8eed570:src/ring.rs) vs the NEW unpark-on-send
/// ring, side by side in one process on the same machine state. The payload IS
/// the producer's push timestamp, so each recv measures true push→recv-return
/// across the thread boundary. The producer sleeps 1 ms between pushes so the
/// consumer is parked (~99.9% of each cycle) when the push lands — the racing
/// case: a word arriving after silence.
#[test]
#[ignore]
fn perf_ring_wake_ab() {
    /// The OLD ring's receive loop, verbatim semantics (no unpark machinery).
    mod old_ring {
        use std::time::Duration;
        const PARK_TIMEOUT: Duration = Duration::from_micros(100);
        const SPIN_ITERS: u32 = 64;

        pub struct Sender(pub rtrb::Producer<u64>);
        pub struct Receiver(pub rtrb::Consumer<u64>);

        pub fn ring(capacity: usize) -> (Sender, Receiver) {
            let (p, c) = rtrb::RingBuffer::new(capacity);
            (Sender(p), Receiver(c))
        }

        impl Sender {
            pub fn send(&mut self, v: u64) -> bool {
                self.0.push(v).is_ok()
            }
        }

        impl Receiver {
            pub fn recv(&mut self) -> Option<u64> {
                loop {
                    for _ in 0..SPIN_ITERS {
                        if let Ok(v) = self.0.pop() {
                            return Some(v);
                        }
                        std::hint::spin_loop();
                    }
                    if self.0.is_abandoned() {
                        return self.0.pop().ok();
                    }
                    std::thread::park_timeout(PARK_TIMEOUT);
                }
            }
        }
    }

    const N: usize = 500;
    const GAP: Duration = Duration::from_millis(1);

    fn stats(mut deltas: Vec<u64>) -> (u64, u64, u64) {
        deltas.sort_unstable();
        (percentile(&deltas, 0.50), percentile(&deltas, 0.95), *deltas.last().unwrap())
    }

    // OLD ring.
    let (mut otx, mut orx) = old_ring::ring(8);
    let old_consumer = std::thread::spawn(move || {
        let mut deltas = Vec::with_capacity(N);
        while let Some(t0) = orx.recv() {
            deltas.push(MonotonicNs::now().0.saturating_sub(t0));
        }
        deltas
    });
    for _ in 0..N {
        std::thread::sleep(GAP);
        assert!(otx.send(MonotonicNs::now().0), "old ring full");
    }
    drop(otx);
    let (o50, o95, omax) = stats(old_consumer.join().unwrap());

    // NEW ring.
    let (mut ntx, mut nrx) = earshot::ring::ring::<u64>(8);
    let new_consumer = std::thread::spawn(move || {
        let mut deltas = Vec::with_capacity(N);
        while let Some(t0) = nrx.recv() {
            deltas.push(MonotonicNs::now().0.saturating_sub(t0));
        }
        deltas
    });
    for _ in 0..N {
        std::thread::sleep(GAP);
        ntx.send(MonotonicNs::now().0).ok().expect("new ring full");
    }
    drop(ntx);
    let (n50, n95, nmax) = stats(new_consumer.join().unwrap());

    println!(
        "ring idle-wake push→recv ({N} parked pushes each):\n\
         old (park-timeout only): p50 {} µs  p95 {} µs  max {} µs\n\
         new (unpark-on-send)   : p50 {} µs  p95 {} µs  max {} µs",
        o50 / 1000, o95 / 1000, omax / 1000, n50 / 1000, n95 / 1000, nmax / 1000
    );
    // The claim under test: the new ring's typical wake beats the old ring's
    // park-timeout-bounded wake. Assert on p50 with wide margin (macOS
    // scheduling noise can spike any single wake).
    assert!(
        n50 < o50,
        "new ring p50 {n50} ns not better than old {o50} ns — unpark not helping"
    );
}

/// Ingress per-frame decode A/B (sweep 2026-07-29): the old two-allocation
/// pattern (decode into a Vec, then copy into `Arc<[i16]>` / drain into a
/// Vec<u8>, then decode) vs the new single-allocation direct-collect paths now
/// used by twilio_ws/sip (`decode_ulaw_arc`) and pipe's `emit`. Interleaved
/// A/B rounds in one process; report-only prints both series — this laptop
/// has ~3.5× run variance, so judge the series, not one number.
#[test]
#[ignore]
fn perf_ingress_decode_ab() {
    use earshot::source::twilio_ws::{decode_ulaw, decode_ulaw_arc};
    let ulaw: Vec<u8> = (0..160u32).map(|i| (i * 7 % 256) as u8).collect();
    let s16: Vec<u8> = (0..640u32).map(|i| (i * 13 % 256) as u8).collect();
    let iters: u64 = 100_000;

    let mut rows: Vec<(u64, u64, u64, u64)> = Vec::new();
    for _round in 0..5 {
        // A: μ-law old (Vec then Arc copy).
        let t0 = Instant::now();
        let mut sink = 0i64;
        for _ in 0..iters {
            let pcm: Arc<[i16]> = Arc::from(decode_ulaw(&ulaw).as_slice());
            sink += pcm[0] as i64;
        }
        let ulaw_old = t0.elapsed().as_nanos() as u64 / iters;
        std::hint::black_box(sink);

        // B: μ-law new (single-allocation collect).
        let t0 = Instant::now();
        let mut sink = 0i64;
        for _ in 0..iters {
            let pcm = decode_ulaw_arc(&ulaw);
            sink += pcm[0] as i64;
        }
        let ulaw_new = t0.elapsed().as_nanos() as u64 / iters;
        std::hint::black_box(sink);

        // A: pipe old (drain into Vec<u8>, decode, Arc copy).
        let t0 = Instant::now();
        let mut sink = 0i64;
        for _ in 0..iters {
            let mut buf = s16.clone();
            let bytes: Vec<u8> = buf.drain(..).collect();
            let pcm: Arc<[i16]> = bytes
                .chunks_exact(2)
                .map(|b| i16::from_le_bytes([b[0], b[1]]))
                .collect();
            sink += pcm[0] as i64;
        }
        let pipe_old = t0.elapsed().as_nanos() as u64 / iters;
        std::hint::black_box(sink);

        // B: pipe new (decode straight off the buffer, then drain).
        let t0 = Instant::now();
        let mut sink = 0i64;
        for _ in 0..iters {
            let mut buf = s16.clone();
            let pcm: Arc<[i16]> = buf[..]
                .chunks_exact(2)
                .map(|b| i16::from_le_bytes([b[0], b[1]]))
                .collect();
            buf.drain(..);
            sink += pcm[0] as i64;
        }
        let pipe_new = t0.elapsed().as_nanos() as u64 / iters;
        std::hint::black_box(sink);

        rows.push((ulaw_old, ulaw_new, pipe_old, pipe_new));
    }
    println!("ingress decode per-frame ns (interleaved rounds; 160-sample μ-law / 320-sample s16):");
    for (i, (uo, un, po, pn)) in rows.iter().enumerate() {
        println!("  round {i}: ulaw old {uo}  new {un}   pipe old {po}  new {pn}");
    }
    // Loose drift alarm only (2–3× convention): the new path must never be
    // grossly slower than the old one.
    let (uo, un): (u64, u64) = (rows.iter().map(|r| r.0).sum(), rows.iter().map(|r| r.1).sum());
    let (po, pn): (u64, u64) = (rows.iter().map(|r| r.2).sum(), rows.iter().map(|r| r.3).sum());
    assert!(un < uo * 2, "decode_ulaw_arc slower than the two-alloc path: {un} vs {uo}");
    assert!(pn < po * 2, "pipe direct-collect slower than the drain-first path: {pn} vs {po}");
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

/// `record_send` sits on the fire path (executor.rs, inside the exec→send
/// edge). Post-ST1 it must be pure in-memory work — the disk write is deferred
/// to the background persister. This is the objective proof no blocking I/O
/// slid back onto the path.
#[test]
#[ignore]
fn perf_risk_record_send() {
    let dir = tempfile::tempdir().unwrap();
    let gate = RiskGate::new(RiskConfig::default(), dir.path()).unwrap();
    gate.arm();
    let n = 100_000u64;
    let t0 = Instant::now();
    for _ in 0..n {
        gate.record_send(1, 1);
    }
    let per_op_ns = t0.elapsed().as_nanos() as u64 / n;
    println!("RiskGate.record_send(): {per_op_ns} ns/op");
    assert!(per_op_ns < 200, "record_send {per_op_ns} ns/op — blocking I/O back on the fire path?");
}
