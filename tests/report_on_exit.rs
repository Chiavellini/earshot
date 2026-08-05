//! A terminated run must ALWAYS leave a record — the 2026-08-05 MELI regression.
//!
//! That afternoon MELI armed, produced 4 detections and sent one real order,
//! then its source hit the pipe idle guard (`src/source/pipe.rs`). `arm()`
//! wrote `report.json` only on a clean pipeline drain, so the error propagated
//! straight past the writer and the event's entire record was lost. On disk,
//! "died after trading" and "never armed" looked identical — and the day's
//! grading could not tell them apart.
//!
//! These tests pin the two halves of the fix:
//!   1. a failing source really does make `run_pipeline` return `Err`, and
//!   2. `telemetry::build_run_json` still produces a complete, writable record
//!      from that failure — carrying the event, the exit reason, and whatever
//!      the executor managed before the source died.
//!
//! `build_run_json` is the single choke point `main.rs::finish_run` calls on
//! EVERY exit path; testing it here is what makes the choke point verifiable
//! without driving the binary.

use std::time::Duration;

use earshot::decision::DecisionTable;
use earshot::detector::stub::EnergyStubDetector;
use earshot::pipeline::preproc::PreprocCfg;
use earshot::pipeline::run_pipeline;
use earshot::source::pipe::PipeSource;
use earshot::telemetry::{build_run_json, ExitReason, ExitRecord};
use earshot::types::SourceId;

const RATE: u32 = 16_000;
const EVENT: &str = "KXEARNINGSMENTIONMELI-26AUG05-WEBCAST";

fn sh(script: &str) -> Vec<String> {
    vec!["/bin/sh".into(), "-c".into(), script.into()]
}

/// The MELI shape end to end: a source that goes silent kills the pipeline,
/// and the run still yields a record naming the event and the reason.
#[test]
fn dead_source_still_produces_a_run_record() {
    // Child emits nothing and sleeps — exactly the "dead webcast stream" case.
    let src = PipeSource::new(sh("sleep 30"), RATE, SourceId(0))
        .unwrap()
        .with_idle_timeout(Duration::from_millis(300));

    let err = run_pipeline(
        EVENT,
        80,
        PreprocCfg { resample_to_hz: Some(RATE) },
        Box::new(src),
        Box::new(EnergyStubDetector::for_standard_fixture()),
        DecisionTable::from_keywords(&[]),
        None,
        Default::default(),
    )
    .expect_err("a source that never emits must fail the pipeline, not return Ok");

    // Before the fix, everything below this line simply never ran.
    let exit = ExitRecord::with(ExitReason::SourceError, err.to_string());
    let json = build_run_json(EVENT, None, None, &exit).unwrap();

    assert_eq!(json["event_id"], EVENT, "a partial record must still name its event");
    assert_eq!(json["exit"]["reason"], "source_error");
    assert!(
        json["exit"]["detail"].as_str().unwrap().contains("idle"),
        "the reason the run died must survive into the record: {}",
        json["exit"]["detail"]
    );
    assert_eq!(json["partial_report"], true, "a report built without a pipeline must say so");
    assert!(json["orders"].is_array(), "orders must always be an array, never absent");
    assert_eq!(json["executor_report_missing"], true);
}

/// The clean path keeps its existing shape and gains the exit record, so
/// nothing that already parses these reports breaks.
#[test]
fn clean_drain_record_keeps_pipeline_fields_and_gains_exit() {
    let src = PipeSource::new(sh("head -c 32000 /dev/zero"), RATE, SourceId(0)).unwrap();
    let report = run_pipeline(
        EVENT,
        80,
        PreprocCfg { resample_to_hz: Some(RATE) },
        Box::new(src),
        Box::new(EnergyStubDetector::for_standard_fixture()),
        DecisionTable::from_keywords(&[]),
        None,
        Default::default(),
    )
    .expect("a well-behaved source drains cleanly");

    let json = build_run_json(EVENT, Some(&report), None, &ExitRecord::clean()).unwrap();

    assert_eq!(json["event_id"], EVENT);
    assert_eq!(json["exit"]["reason"], "clean_drain");
    assert!(json["exit"].get("detail").is_none(), "a clean exit carries no detail");
    assert!(json.get("partial_report").is_none(), "a full report is not partial");
    assert!(json["frames_in"].as_u64().unwrap() > 0, "pipeline counters must survive");
    assert!(json["drops"].is_object(), "existing report keys must be untouched");
}

/// `AllLegsDied` is a different diagnosis from a single source failing; a
/// fanout run that loses every leg must not be filed as an ordinary source
/// error, or the post-mortem chases the wrong thing.
#[test]
fn all_legs_died_is_distinguishable_from_a_single_source_error() {
    let a = build_run_json(
        EVENT,
        None,
        None,
        &ExitRecord::with(ExitReason::AllLegsDied, "every leg died"),
    )
    .unwrap();
    let b = build_run_json(
        EVENT,
        None,
        None,
        &ExitRecord::with(ExitReason::SourceError, "pipe source idle past 30s"),
    )
    .unwrap();
    assert_eq!(a["exit"]["reason"], "all_legs_died");
    assert_eq!(b["exit"]["reason"], "source_error");
    assert_ne!(a["exit"]["reason"], b["exit"]["reason"]);
}
