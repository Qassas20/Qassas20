"""Unit tests for the signal engine (Integration Spec §2) — bars -> signals."""

import unittest

from trading_bot.broker.base import Bar, Side
from trading_bot.signals.engine import RuleSignalEngine, Signal


def make_bars(closes, volumes=None):
    volumes = volumes or [100.0] * len(closes)
    return [
        Bar(symbol="TEST", timestamp_ms=i * 60_000, open=c, high=c + 0.5,
            low=c - 0.5, close=c, volume=v)
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


class TestRuleSignalEngine(unittest.TestCase):
    def setUp(self):
        self.engine = RuleSignalEngine()

    def test_uptrend_emits_buy(self):
        # Accelerating uptrend -> EMA and MACD both bullish (momentum, not just level).
        closes = [100.0 + 0.05 * i * i for i in range(60)]
        vols = [100.0] * 59 + [500.0]  # volume spike on the last bar
        signal = self.engine.evaluate("TEST", make_bars(closes, vols))
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, Side.BUY)
        self.assertGreater(signal.confidence, 0.0)
        self.assertLessEqual(signal.confidence, 1.0)

    def test_downtrend_emits_sell(self):
        closes = [280.0 - 0.05 * i * i for i in range(60)]
        signal = self.engine.evaluate("TEST", make_bars(closes))
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, Side.SELL)

    def test_flat_market_no_signal(self):
        closes = [100.0] * 60
        self.assertIsNone(self.engine.evaluate("TEST", make_bars(closes)))

    def test_insufficient_bars_no_signal(self):
        closes = [100.0 + i for i in range(10)]
        self.assertIsNone(self.engine.evaluate("TEST", make_bars(closes)))

    def test_reason_is_populated(self):
        closes = [100.0 + 0.05 * i * i for i in range(60)]
        vols = [100.0] * 59 + [500.0]
        signal = self.engine.evaluate("TEST", make_bars(closes, vols))
        self.assertTrue(signal.reason)
        self.assertIn("EMA", signal.reason)


if __name__ == "__main__":
    unittest.main()
