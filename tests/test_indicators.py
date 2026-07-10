"""Unit tests for TA indicators (Integration Spec §2) — known bars -> values."""

import unittest

from trading_bot.signals import indicators as ind


class TestEMA(unittest.TestCase):
    def test_flat_series_equals_value(self):
        self.assertEqual(ind.ema([5, 5, 5, 5], 2), [5.0, 5.0, 5.0])

    def test_known_values(self):
        # period 3 seed = mean(1,2,3)=2; next = (4-2)*0.5+2 = 3
        self.assertEqual(ind.ema([1, 2, 3, 4], 3), [2.0, 3.0])

    def test_too_short(self):
        self.assertEqual(ind.ema([1, 2], 5), [])

    def test_bad_period(self):
        with self.assertRaises(ValueError):
            ind.ema([1, 2, 3], 0)


class TestRSI(unittest.TestCase):
    def test_all_gains_is_100(self):
        closes = list(range(1, 20))  # strictly increasing
        vals = ind.rsi(closes, 14)
        self.assertTrue(vals)
        self.assertAlmostEqual(vals[-1], 100.0)

    def test_all_losses_is_zero(self):
        closes = list(range(20, 1, -1))  # strictly decreasing
        vals = ind.rsi(closes, 14)
        self.assertAlmostEqual(vals[-1], 0.0)

    def test_bounds(self):
        closes = [10, 11, 10, 12, 11, 13, 12, 14, 13, 15, 14, 16, 15, 17, 16, 18]
        for v in ind.rsi(closes, 14):
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 100.0)

    def test_too_short(self):
        self.assertEqual(ind.rsi([1, 2, 3], 14), [])


class TestMACD(unittest.TestCase):
    def test_uptrend_positive_macd(self):
        closes = [float(i) for i in range(1, 60)]
        res = ind.macd(closes)
        self.assertTrue(res.macd)
        # In a steady uptrend the fast EMA leads -> MACD line positive
        self.assertGreater(res.macd[-1], 0)

    def test_aligned_lengths(self):
        closes = [float(i % 7) + i * 0.1 for i in range(80)]
        res = ind.macd(closes)
        self.assertEqual(len(res.macd), len(res.signal))
        self.assertEqual(len(res.signal), len(res.histogram))

    def test_insufficient(self):
        res = ind.macd([1, 2, 3])
        self.assertEqual(res.macd, [])


class TestVolume(unittest.TestCase):
    def test_spike_confirms(self):
        vols = [100.0] * 20 + [200.0]
        self.assertTrue(ind.volume_confirms(vols, 20, 1.2))

    def test_flat_does_not_confirm(self):
        vols = [100.0] * 21
        self.assertFalse(ind.volume_confirms(vols, 20, 1.2))

    def test_insufficient(self):
        self.assertFalse(ind.volume_confirms([1, 2], 20))


class TestSupportResistance(unittest.TestCase):
    def test_swing_levels(self):
        highs = [10, 12, 11, 13, 9]
        lows = [8, 9, 7, 10, 6]
        sr = ind.support_resistance(highs, lows, 5)
        self.assertEqual(sr.resistance, 13)
        self.assertEqual(sr.support, 6)

    def test_empty(self):
        sr = ind.support_resistance([], [], 5)
        self.assertIsNone(sr.support)


if __name__ == "__main__":
    unittest.main()
