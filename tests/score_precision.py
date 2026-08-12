#!/usr/bin/env python3
"""Focused, offline regressions for score-precision clock alignment."""

import importlib.util
from pathlib import Path
import unittest


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


if __name__ == "__main__":
    unittest.main()
