"""Unit tests for regime tagging (UAT addendum Section 3.5)."""

import unittest

from trading_bot.calibration import regime as R


class TestMovingAverage(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(R.moving_average([1, 2, 3, 4], 2), [1.5, 2.5, 3.5])

    def test_too_short(self):
        self.assertEqual(R.moving_average([1], 5), [])


class TestMaSlopeRegime(unittest.TestCase):
    def test_steady_uptrend_is_trending(self):
        closes = list(range(1, 60))  # strictly increasing
        self.assertEqual(R.ma_slope_regime(closes, window=5), "trending")

    def test_oscillation_is_choppy(self):
        closes = [10, 11, 10, 11, 10, 11] * 10
        self.assertEqual(R.ma_slope_regime(closes, window=3), "choppy")

    def test_insufficient_data_is_choppy(self):
        self.assertEqual(R.ma_slope_regime([1, 2], window=20), "choppy")


class TestADX(unittest.TestCase):
    def test_strong_trend_high_adx(self):
        n = 60
        highs = [100 + i for i in range(n)]
        lows = [99 + i for i in range(n)]
        closes = [99.5 + i for i in range(n)]
        self.assertEqual(R.adx_regime(highs, lows, closes, period=14), "trending")

    def test_flat_market_low_adx(self):
        n = 60
        highs = [100.5, 100.4] * (n // 2)
        lows = [99.5, 99.6] * (n // 2)
        closes = [100.0, 100.0] * (n // 2)
        self.assertEqual(R.adx_regime(highs, lows, closes, period=14), "choppy")

    def test_insufficient_data(self):
        self.assertIsNone(R.compute_adx([1, 2], [1, 2], [1, 2], period=14))
        self.assertEqual(R.adx_regime([1, 2], [1, 2], [1, 2]), "choppy")


if __name__ == "__main__":
    unittest.main()
