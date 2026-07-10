"""Unit tests for reporting metrics (UAT addendum Section 4)."""

import math
import unittest

from trading_bot.reporting import metrics as M


def trade(adjusted, raw=None, slippage=0.0, fees=0.0, haircut=0.0,
          signal=100.0, fill=100.0, regime="trending"):
    return {
        "adjusted_pnl": adjusted,
        "raw_pnl": raw if raw is not None else adjusted,
        "modeled_slippage": slippage,
        "fees": fees,
        "haircut_qty": haircut,
        "signal_price": signal,
        "modeled_fill_price": fill,
        "regime": regime,
    }


class TestExpectancy(unittest.TestCase):
    def test_mean_adjusted(self):
        trades = [trade(10), trade(-4), trade(6)]
        self.assertAlmostEqual(M.expectancy(trades), 4.0)

    def test_empty(self):
        self.assertEqual(M.expectancy([]), 0.0)

    def test_uses_adjusted_not_raw(self):
        # raw is rosy, adjusted is the truth
        trades = [trade(adjusted=1, raw=100), trade(adjusted=-1, raw=100)]
        self.assertAlmostEqual(M.expectancy(trades), 0.0)


class TestProfitFactor(unittest.TestCase):
    def test_basic(self):
        trades = [trade(10), trade(20), trade(-10)]
        self.assertAlmostEqual(M.profit_factor(trades), 3.0)

    def test_no_losses_is_inf(self):
        self.assertEqual(M.profit_factor([trade(5), trade(5)]), math.inf)

    def test_no_profit_is_zero(self):
        self.assertEqual(M.profit_factor([trade(-5)]), 0.0)


class TestMaxDrawdown(unittest.TestCase):
    def test_drawdown(self):
        # equity: 10, 5, 15, 3 -> peak 15, trough 3 -> dd 12
        trades = [trade(10), trade(-5), trade(10), trade(-12)]
        self.assertAlmostEqual(M.max_drawdown(trades), 12.0)

    def test_monotonic_no_drawdown(self):
        self.assertEqual(M.max_drawdown([trade(5), trade(5)]), 0.0)


class TestCoreMetrics(unittest.TestCase):
    def test_win_rate_and_averages(self):
        trades = [trade(10), trade(20), trade(-5), trade(-15)]
        m = M.core_metrics(trades)
        self.assertEqual(m.trade_count, 4)
        self.assertEqual(m.wins, 2)
        self.assertEqual(m.losses, 2)
        self.assertAlmostEqual(m.win_rate, 0.5)
        self.assertAlmostEqual(m.avg_win, 15.0)
        self.assertAlmostEqual(m.avg_loss, 10.0)  # positive magnitude
        self.assertAlmostEqual(m.expectancy, 2.5)

    def test_expectancy_formula_identity(self):
        # (win% * avg_win) - (loss% * avg_loss) == mean adjusted
        trades = [trade(10), trade(20), trade(-5), trade(-15)]
        m = M.core_metrics(trades)
        formula = m.win_rate * m.avg_win - (m.losses / m.trade_count) * m.avg_loss
        self.assertAlmostEqual(formula, m.expectancy)


class TestRealityGap(unittest.TestCase):
    def test_gap_and_slippage(self):
        trades = [
            trade(adjusted=8, raw=10, slippage=0.5, fees=0.1, haircut=5),
            trade(adjusted=4, raw=10, slippage=0.5, fees=0.1, haircut=15),
        ]
        g = M.reality_gap_metrics(trades)
        self.assertAlmostEqual(g.total_raw_pnl, 20)
        self.assertAlmostEqual(g.total_adjusted_pnl, 12)
        self.assertAlmostEqual(g.raw_vs_adjusted_gap, 8)
        self.assertAlmostEqual(g.cumulative_slippage_cost, 1.0)
        self.assertAlmostEqual(g.modeled_slippage_per_trade, 0.5)
        self.assertAlmostEqual(g.haircut_qty_total, 20)
        self.assertAlmostEqual(g.total_fees, 0.2)

    def test_fill_drift_bps(self):
        # signal 100, fill 100.5 -> 50 bps
        trades = [trade(adjusted=1, signal=100.0, fill=100.5)]
        g = M.reality_gap_metrics(trades)
        self.assertAlmostEqual(g.avg_fill_drift_bps, 50.0)


class TestRegimeSplit(unittest.TestCase):
    def test_split(self):
        trades = [
            trade(10, regime="trending"),
            trade(20, regime="trending"),
            trade(-5, regime="choppy"),
        ]
        split = M.regime_split(trades)
        self.assertEqual(split.trending.trade_count, 2)
        self.assertEqual(split.choppy.trade_count, 1)
        self.assertAlmostEqual(split.trending.expectancy, 15.0)
        self.assertAlmostEqual(split.choppy.expectancy, -5.0)


class TestForecast(unittest.TestCase):
    def test_scales_from_expectancy(self):
        trades = [trade(2), trade(2)]  # expectancy 2
        fc = M.forecast(trades, trades_per_day=10)
        self.assertAlmostEqual(fc["daily"], 20)
        self.assertAlmostEqual(fc["weekly"], 100)
        self.assertAlmostEqual(fc["monthly"], 420)
        self.assertAlmostEqual(fc["yearly"], 5040)


if __name__ == "__main__":
    unittest.main()
