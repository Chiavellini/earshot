#!/usr/bin/env python3
"""Focused, offline regressions for score-precision clock alignment."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "score_precision", REPO / "scripts" / "score-precision.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class OffsetAlignmentTests(unittest.TestCase):
    def test_long_recorder_lead_uses_cross_keyword_support(self):
        rows = [
            {"t": 96.45, "occ": [177.0, 2313.17]},
            {"t": 1266.68, "occ": [3472.94]},
        ]
        offset, residuals, support, usable = MODULE.estimate_offset(rows, 20.0)
        self.assertAlmostEqual(offset, 2211.49, places=2)
        self.assertEqual((support, usable), (2, 2))
        self.assertTrue(all(abs(value) < 6 for value in residuals))

    def test_disjoint_equally_supported_offsets_are_refused(self):
        rows = [
            {"t": 0.0, "occ": [100.0, 1000.0]},
            {"t": 10.0, "occ": [110.0, 1010.0]},
        ]
        with self.assertRaisesRegex(MODULE.AlignmentError, "disjoint clusters"):
            MODULE.estimate_offset(rows, 20.0)

    def test_two_intents_are_required_when_two_are_available(self):
        rows = [
            {"t": 0.0, "occ": [100.0]},
            {"t": 50.0, "occ": [1000.0]},
        ]
        with self.assertRaisesRegex(MODULE.AlignmentError, "need at least 2"):
            MODULE.estimate_offset(rows, 20.0)

    def test_no_occurrences_are_refused(self):
        with self.assertRaisesRegex(MODULE.AlignmentError, "no candidate hits"):
            MODULE.estimate_offset([{"t": 1.0, "occ": []}], 20.0)

    def test_asr_hyphen_tokenization_normalizes_to_phrase(self):
        self.assertEqual(MODULE.normalized_words("Multi -rail, China."), "multi rail china")

    def test_timed_score_retains_alignment_and_per_intent_diagnostics(self):
        rows = [
            {"ticker": "A", "phrase": "alpha", "t": 0.0, "occ": [100.0],
             "cap": 97, "size": 10, "sent": True},
            {"ticker": "B", "phrase": "beta", "t": 10.0, "occ": [110.0],
             "cap": 50, "size": 2, "sent": False},
            {"ticker": "C", "phrase": "gamma", "t": 20.0, "occ": [],
             "cap": 1, "size": 1, "sent": False},
        ]
        score = MODULE.score_rows(rows, True, 20.0)
        self.assertEqual(score["precision"], {
            "status": "scored",
            "true_positive": 2,
            "false_positive": 1,
            "denominator": 3,
            "ratio": 2 / 3,
        })
        self.assertEqual(score["alignment"]["offset_seconds"], 100.0)
        self.assertEqual(score["alignment"]["residual_spread_seconds"], 0.0)
        self.assertEqual(score["alignment"]["support"], 2)
        self.assertEqual(score["rows"][2]["verdict"], "false_positive_never_said")

    def test_presence_only_never_becomes_numeric_precision(self):
        rows = [
            {"ticker": "A", "phrase": "alpha", "occ": 2, "sent": True},
            {"ticker": "B", "phrase": "beta", "occ": 0, "sent": False},
        ]
        score = MODULE.score_rows(rows, False, 20.0)
        self.assertEqual(score["precision"]["status"], "not_resolvable")
        self.assertEqual(score["settlement_presence"], {"said": 1, "total": 2})

    def test_precision_artifact_is_hash_bound_and_exclusive_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            run.mkdir()
            context = {
                "run": run,
                "plan": {
                    "event_id": "KXEARNINGSMENTIONTEST-26AUG20-WEBCAST",
                    "run_id": "fixture-run",
                    "plan_sha256": "a" * 64,
                    "config_path": "configs/test.toml",
                    "artifacts": {"runtime_report": "report/report.json"},
                },
                "config_sha256": "b" * 64,
                "report_sha256": "c" * 64,
            }
            truth = {"kind": "timed-transcript-json", "sha256": "d" * 64}
            scoring = {
                "mode": "timed",
                "precision": {"status": "scored", "true_positive": 1,
                              "false_positive": 0, "denominator": 1, "ratio": 1.0},
                "settlement_presence": None,
                "alignment": {"status": "established", "window_seconds": 20.0,
                              "offset_seconds": 4.0, "residual_spread_seconds": 0.0,
                              "support": 1, "usable_intents": 1,
                              "support_residuals_seconds": [0.0]},
                "rows": [],
            }
            artifact = MODULE.precision_artifact(context, truth, scoring)
            unsigned = dict(artifact)
            digest = unsigned.pop("score_sha256")
            self.assertEqual(digest, MODULE.sha256_bytes(MODULE.canonical_bytes(unsigned)))
            path, state = MODULE.publish_precision_artifact(context, artifact)
            self.assertEqual(state, "published")
            self.assertEqual(json.loads(path.read_text()), artifact)
            self.assertEqual(MODULE.publish_precision_artifact(context, artifact)[1],
                             "existing-identical")
            changed = json.loads(json.dumps(artifact))
            changed["inputs"]["truth"]["sha256"] = "e" * 64
            unsigned = dict(changed)
            unsigned.pop("score_sha256")
            changed["score_sha256"] = MODULE.sha256_bytes(
                MODULE.canonical_bytes(unsigned))
            with self.assertRaisesRegex(ValueError, "differs"):
                MODULE.publish_precision_artifact(context, changed)

    def test_typed_context_binds_runplan_config_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            event, run_id = "KXEARNINGSMENTIONTEST-26AUG20-WEBCAST", "run-1"
            run = repo / "var" / "runs" / event / run_id
            (run / "control").mkdir(parents=True)
            (run / "report").mkdir()
            (repo / "configs").mkdir()
            (repo / "scripts").mkdir()
            (repo / "scripts" / "eventctl.py").write_text("# fixture\n")
            config = repo / "configs" / "test.toml"
            config.write_text(f'[event]\nid = "{event}"\n')
            report = run / "report" / "report.json"
            report.write_text(json.dumps({"event_id": event, "run_id": run_id}))
            plan = {
                "schema": 3,
                "kind": "run-plan",
                "event_id": event,
                "run_id": run_id,
                "plan_sha256": "a" * 64,
                "config_path": "configs/test.toml",
                "config_sha256": MODULE.sha256_file(config),
                "artifacts": {"runtime_report": "report/report.json"},
            }
            (run / "control" / "run-plan.json").write_text(json.dumps(plan))
            ok = mock.Mock(returncode=0, stdout="", stderr="")
            previous = MODULE.REPO
            MODULE.REPO = str(repo)
            try:
                with mock.patch.object(MODULE.subprocess, "run", return_value=ok):
                    context = MODULE.typed_context(run)
                    self.assertEqual(context["report_sha256"], MODULE.sha256_file(report))
                    config.write_text("mutated\n")
                    with self.assertRaisesRegex(ValueError, "fingerprint differs"):
                        MODULE.typed_context(run)
            finally:
                MODULE.REPO = previous


if __name__ == "__main__":
    unittest.main()
