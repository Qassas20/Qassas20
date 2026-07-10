"""Unit tests for shadow-live gap analysis (UAT addendum Section 6)."""

import unittest

from trading_bot.calibration import shadow


def row(paper, live):
    return {"paper_adjusted_pnl": paper, "live_actual_pnl": live, "gap": live - paper}


class TestSummarize(unittest.TestCase):
    def test_empty(self):
        s = shadow.summarize([])
        self.assertEqual(s.n, 0)
        self.assertIn("No shadow-live data", shadow.calibration_hint(s))

    def test_model_optimistic(self):
        # live consistently worse than paper -> model too optimistic
        rows = [row(100, 80), row(100, 85), row(100, 78)]
        s = shadow.summarize(rows)
        self.assertLess(s.mean_gap, 0)
        self.assertEqual(s.direction, "model_optimistic")
        self.assertIn("increase impact_k", shadow.calibration_hint(s))

    def test_model_pessimistic(self):
        rows = [row(80, 100), row(80, 105)]
        s = shadow.summarize(rows)
        self.assertGreater(s.mean_gap, 0)
        self.assertEqual(s.direction, "model_pessimistic")

    def test_aligned(self):
        rows = [row(100, 100.5), row(100, 99.5)]
        s = shadow.summarize(rows, tolerance=0.05)
        self.assertEqual(s.direction, "aligned")

    def test_cumulative_gap(self):
        rows = [row(100, 90), row(100, 95)]
        s = shadow.summarize(rows)
        self.assertAlmostEqual(s.cumulative_gap, -15)


if __name__ == "__main__":
    unittest.main()
