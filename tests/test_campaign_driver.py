import unittest
from collections import deque

from campaign_driver import (
    BaselineProfile,
    CaptureResult,
    GlitchConfig,
    classify_attempt,
    compute_distance_metrics,
    compute_energy_score,
    describe_pattern,
    has_interesting_region,
    normalized_edit_distance,
    normalize_uart_text,
    seed_patterns,
)


class CampaignDriverUnitTests(unittest.TestCase):
    def test_describe_pattern(self):
        desc = describe_pattern(0b00101100, 8)
        self.assertEqual(desc["hamming_weight"], 3)
        self.assertEqual(desc["max_run_length"], 2)
        self.assertEqual(desc["run_count"], 2)
        self.assertEqual(desc["leading_zero_count"], 2)
        self.assertEqual(desc["trailing_zero_count"], 2)

    def test_normalization(self):
        out = normalize_uart_text("12:00:01 boot\r\n\r\nVALUE=123")
        self.assertIn("<time>", out)
        self.assertNotIn("\\r", out)

    def test_normalized_edit_distance(self):
        self.assertEqual(normalized_edit_distance("abc", "abc"), 0.0)
        self.assertGreater(normalized_edit_distance("abc", "xyz"), 0.8)

    def test_classification_reset_and_survived(self):
        baseline = BaselineProfile(
            raw_samples=[],
            normalized_samples=["ok"],
            canonical_output="ok",
            normal_duration_ms_median=80.0,
            time_to_first_byte_ms_median=10.0,
            known_reset_markers=["boot", "reset"],
        )
        reset_cap = CaptureResult(
            raw=b"BOOT reset",
            text="BOOT reset",
            normalized="boot reset",
            time_to_first_byte_ms=30.0,
            response_duration_ms=160.0,
            line_count=1,
        )
        reset_dist = compute_distance_metrics(reset_cap, baseline)
        reset_cls = classify_attempt(reset_cap, baseline, {"raw": ""}, reset_dist)
        self.assertEqual(reset_cls.label, "RESET_ARTIFACT")

        ok_cap = CaptureResult(
            raw=b"ok",
            text="ok",
            normalized="ok",
            time_to_first_byte_ms=10.5,
            response_duration_ms=78.0,
            line_count=1,
        )
        ok_dist = compute_distance_metrics(ok_cap, baseline)
        ok_cls = classify_attempt(ok_cap, baseline, {"raw": ""}, ok_dist)
        self.assertEqual(ok_cls.label, "SURVIVED")

    def test_energy_score_increases_with_density(self):
        low = GlitchConfig(1, 100, 0b00000001, 8, 250000000, None, 20, 0, "seed")
        high = GlitchConfig(1, 100, 0b11110000, 8, 250000000, None, 20, 0, "seed")
        self.assertLess(
            compute_energy_score(low, describe_pattern(low.pattern, low.pattern_width)),
            compute_energy_score(high, describe_pattern(high.pattern, high.pattern_width)),
        )

    def test_seed_patterns_are_sparse_and_nonzero(self):
        pats = seed_patterns(8)
        self.assertTrue(pats)
        for pat, _ in pats:
            self.assertNotEqual(pat, 0)
            self.assertNotEqual(pat, 0xFF)

    def test_interesting_region_detection(self):
        recent = deque(
            [
                {"classification": {"label": "KILLED"}},
                {"classification": {"label": "KILLED"}},
                {"classification": {"label": "KILLED"}},
                {"classification": {"label": "SURVIVED"}},
            ],
            maxlen=10,
        )
        self.assertTrue(has_interesting_region(recent, required_killed=3, max_reset_rate=0.3))


if __name__ == "__main__":
    unittest.main()
