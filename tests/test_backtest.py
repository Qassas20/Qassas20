"""Unit tests for the backtest harness."""

import unittest

from trading_bot.backtest.data import synthetic_bars, trending_bars
from trading_bot.backtest.engine import Backtester, BacktestConfig
from trading_bot.broker.base import Bar, Side
from trading_bot.calibration.config import CalibrationConfig, FeeSchedule
from trading_bot.db.database import CalibrationDB
from trading_bot.signals.engine import RuleSignalEngine, Signal, SignalEngine


class AlwaysBuy(SignalEngine):
    """Deterministic engine: buy once, then hold (no re-entry while open)."""

    def evaluate(self, symbol, bars):
        return Signal(symbol, Side.BUY, 0.9, "forced")


class TestBacktesterMechanics(unittest.TestCase):
    def setUp(self):
        self.calib = CalibrationConfig(liquidity_cap_pct=1.0, latency_ms=0,
                                       fees=FeeSchedule(0, 0, 0))

    def test_take_profit_exit(self):
        # Flat then a jump above the 4% target -> exit at target.
        bars = [Bar("X", i * 60_000, 100, 100.5, 99.5, 100.0, 10_000) for i in range(60)]
        bars.append(Bar("X", 60 * 60_000, 100, 106, 100, 105.0, 10_000))  # spikes through +4%
        bt = Backtester(AlwaysBuy(), self.calib,
                        BacktestConfig(warmup_bars=50, take_profit_pct=0.04,
                                       exit_on_opposite=False))
        result = bt.run({"X": bars})
        self.assertEqual(len(result.trades), 1)
        t = result.trades[0]
        # entry ~100, exit at target 104 -> raw pnl positive
        self.assertGreater(t["raw_pnl"], 0)
        # friction makes adjusted worse than raw
        self.assertLess(t["adjusted_pnl"], t["raw_pnl"])

    def test_stop_loss_exit(self):
        bars = [Bar("X", i * 60_000, 100, 100.5, 99.5, 100.0, 10_000) for i in range(60)]
        bars.append(Bar("X", 60 * 60_000, 100, 100, 97, 97.5, 10_000))  # drops through -2%
        bt = Backtester(AlwaysBuy(), self.calib,
                        BacktestConfig(warmup_bars=50, stop_loss_pct=0.02,
                                       exit_on_opposite=False))
        result = bt.run({"X": bars})
        self.assertEqual(len(result.trades), 1)
        self.assertLess(result.trades[0]["raw_pnl"], 0)  # stopped out at a loss

    def test_force_close_at_end(self):
        bars = [Bar("X", i * 60_000, 100, 100.5, 99.5, 100.0, 10_000) for i in range(70)]
        bt = Backtester(AlwaysBuy(), self.calib,
                        BacktestConfig(warmup_bars=50, max_holding_bars=1000,
                                       exit_on_opposite=False))
        result = bt.run({"X": bars})
        # position opened after warmup, never hit stop/target -> closed at end
        self.assertEqual(len(result.trades), 1)

    def test_max_holding_exit(self):
        bars = [Bar("X", i * 60_000, 100, 100.4, 99.6, 100.0, 10_000) for i in range(120)]
        bt = Backtester(AlwaysBuy(), self.calib,
                        BacktestConfig(warmup_bars=50, max_holding_bars=5,
                                       exit_on_opposite=False))
        result = bt.run({"X": bars})
        self.assertGreaterEqual(len(result.trades), 1)


class TestBacktesterReport(unittest.TestCase):
    def test_produces_report_and_persists(self):
        calib = CalibrationConfig()
        bars = synthetic_bars("AAPL", n=400, seed=7)
        bt = Backtester(RuleSignalEngine(), calib, BacktestConfig(warmup_bars=60))
        db = CalibrationDB(":memory:")
        db.add_bucket("bt")
        result = bt.run({"AAPL": bars}, db=db, bucket_id=1)
        # Report and forecast exist iff any trades were taken.
        if result.trades:
            self.assertIsNotNone(result.report)
            self.assertEqual(result.report.core.trade_count, len(result.trades))
            self.assertIn("yearly", result.forecast)
            # persisted rows match
            self.assertEqual(len(db.trades_for_bucket(1)), len(result.trades))
            # summary renders without error
            self.assertIn("trades=", result.summary())

    def test_multi_symbol_aggregates(self):
        calib = CalibrationConfig()
        data = {
            "UP": trending_bars("UP", n=400, seed=1, up=True),
            "DN": trending_bars("DN", n=400, seed=2, up=False),
        }
        bt = Backtester(RuleSignalEngine(), calib, BacktestConfig(warmup_bars=60))
        result = bt.run(data)
        # Aggregated trades come from both symbols (if any triggered)
        symbols = {t["symbol"] for t in result.trades}
        self.assertTrue(symbols.issubset({"UP", "DN"}))

    def test_no_trades_is_safe(self):
        calib = CalibrationConfig()
        # Perfectly flat -> RuleSignalEngine emits nothing.
        flat = [Bar("F", i * 60_000, 100, 100, 100, 100.0, 10_000) for i in range(200)]
        bt = Backtester(RuleSignalEngine(), calib)
        result = bt.run({"F": flat})
        self.assertEqual(result.trades, [])
        self.assertEqual(result.summary(), "no trades")


class TestNoLookahead(unittest.TestCase):
    def test_signal_only_sees_past_bars(self):
        seen_lengths = []

        class Recording(SignalEngine):
            def evaluate(self, symbol, bars):
                seen_lengths.append(len(bars))
                return None

        bars = synthetic_bars("X", n=100, seed=3)
        bt = Backtester(Recording(), CalibrationConfig(), BacktestConfig(warmup_bars=50))
        bt.run({"X": bars})
        # First evaluation window is warmup+1 bars; grows by 1; never exceeds n.
        self.assertEqual(seen_lengths[0], 51)
        self.assertTrue(all(a <= b for a, b in zip(seen_lengths, seen_lengths[1:])))
        self.assertLessEqual(max(seen_lengths), 100)


if __name__ == "__main__":
    unittest.main()
