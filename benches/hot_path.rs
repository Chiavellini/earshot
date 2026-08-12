//! Hot-path microbenchmarks for the latency-reduction pass.
//!
//! These pin p50/p99 baselines for the small clone/copy/record costs that sit
//! directly on the detect→fire critical path, so WS-4/5/6 changes can be A/B'd
//! against a stable number instead of a one-shot dev-laptop reading.
//!
//! What's here and why:
//!   * `decision_intent_clone` — the per-fire clone at the ring handoff
//!     (`pipeline::run_pipeline` → `i.clone()`), dominated by the `String`
//!     ticker copy. WS-6 replaces that `String` with an `Arc<str>`/index; this
//!     bench is the before/after gauge.
//!   * `chunk_seal_copy` — the chunker's `Arc::<[i16]>::from(&buf)` per sealed
//!     chunk (`pipeline/chunker.rs` seal), a full PCM copy every `chunk_ms`.
//!     WS-6's FramePool targets this allocation+copy.
//!   * `recorder_record` — `LatencyRecorder::record`, the telemetry cost paid
//!     several times per chunk on the detect thread (WS-5 moves these off the
//!     critical section; this is the per-op floor).
//!
//! The fire-path micro-costs that need live wiring (Presigner::get, exec→send,
//! the pre-staged body clone) are already covered by the ignored
//! `perf_executor_fire_path` test against a wiremock; not duplicated here.
//!
//! Run: `cargo bench --bench hot_path`

use std::sync::Arc;

use criterion::{black_box, criterion_group, criterion_main, BenchmarkId, Criterion, Throughput};

use earshot::clock::MonotonicNs;
use earshot::telemetry::LatencyRecorder;
use earshot::types::{DecisionIntent, KeywordId, Tier};

fn sample_intent() -> DecisionIntent {
    let now = MonotonicNs::now();
    DecisionIntent {
        keyword: KeywordId(0),
        source: earshot::types::SourceId(0),
        // A realistic Kalshi ticker length — the clone cost tracks this String.
        market_ticker: "KXEARNINGSMENTIONMS-26JUL15-RECE".to_string(),
        max_price_cents: 97,
        tranche_contracts: 200,
        tier: Tier::Confirmed,
        audio_ts: now,
        decision_ts: now,
        handoff_ts: now,
    }
}

fn bench_decision_intent_clone(c: &mut Criterion) {
    let intent = sample_intent();
    c.bench_function("decision_intent_clone", |b| {
        b.iter(|| black_box(black_box(&intent).clone()));
    });
}

fn bench_chunk_seal_copy(c: &mut Criterion) {
    let mut g = c.benchmark_group("chunk_seal_copy");
    // Detector-side sample rate is 16 kHz (preproc upsamples 8→16). Prod
    // chunk_ms is 80 → 1280 samples; the WS-2 candidates (40/20 ms) seal more
    // often but copy less each, so bench the span.
    for &samples in &[
        320usize, /*20ms*/
        640,      /*40ms*/
        1280,     /*80ms*/
    ] {
        let buf: Vec<i16> = (0..samples as i16).collect();
        g.throughput(Throughput::Elements(samples as u64));
        g.bench_with_input(BenchmarkId::from_parameter(samples), &buf, |b, buf| {
            b.iter(|| {
                let arc: Arc<[i16]> = Arc::from(black_box(buf.as_slice()));
                black_box(arc)
            });
        });
    }
    g.finish();
}

fn bench_recorder_record(c: &mut Criterion) {
    let mut r = LatencyRecorder::new();
    c.bench_function("recorder_record", |b| {
        b.iter(|| r.record(black_box("capture→decision"), black_box(91_000_000)));
    });
}

criterion_group!(
    hot_path,
    bench_decision_intent_clone,
    bench_chunk_seal_copy,
    bench_recorder_record
);
criterion_main!(hot_path);
