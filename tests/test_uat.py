"""Unit tests for UAT gates (UAT addendum Section 5)."""

import unittest

from trading_bot.reporting import uat
from trading_bot.reporting.uat import Verdict


def trade(adjusted, regime="trending"):
    return {"adjusted_pnl": adjusted, "raw_pnl": adjusted, "regime": regime}


def make_passing_sample():
    """>=100 trades, PF>1.3, positive in both regimes, drawdown small."""
    trades = []
    # trending: mostly winners
    for _ in range(50):
        trades.append(trade(10, "trending"))
    for _ in range(10):
        trades.append(trade(-5, "trending"))
    # choppy: slightly positive
    for _ in range(30):
        trades.append(trade(4, "choppy"))
    for _ in range(20):
        trades.append(trade(-2, "choppy"))
    return trades


class TestPass(unittest.TestCase):
    def test_full_pass(self):
        res = uat.evaluate_bucket(
            make_passing_sample(),
            max_drawdown_limit=1000,
            weeks_observed=4,
            saw_trending_week=True,
            saw_choppy_week=True,
        )
        self.assertEqual(res.verdict, Verdict.PASS)
        self.assertTrue(res.go_live)


class TestKill(unittest.TestCase):
    def test_negative_expectancy_kills(self):
        trades = [trade(-1) for _ in range(150)]
        res = uat.evaluate_bucket(trades, max_drawdown_limit=10_000, weeks_observed=4)
        self.assertEqual(res.verdict, Verdict.KILL)
        self.assertFalse(res.go_live)
        self.assertTrue(any("expectancy" in r for r in res.reasons))

    def test_drawdown_breach_kills(self):
        # positive expectancy overall but a deep trough
        trades = [trade(100)] + [trade(-40) for _ in range(3)] + [trade(100) for _ in range(120)]
        res = uat.evaluate_bucket(trades, max_drawdown_limit=50, weeks_observed=4)
        self.assertEqual(res.verdict, Verdict.KILL)


class TestInsufficient(unittest.TestCase):
    def test_too_few_trades(self):
        trades = [trade(10) for _ in range(20)]
        res = uat.evaluate_bucket(trades, max_drawdown_limit=10_000, weeks_observed=4)
        self.assertEqual(res.verdict, Verdict.INSUFFICIENT)
        self.assertTrue(any("trades" in r for r in res.reasons))

    def test_missing_regime_coverage(self):
        trades = make_passing_sample()
        res = uat.evaluate_bucket(
            trades, max_drawdown_limit=10_000, weeks_observed=4,
            saw_trending_week=True, saw_choppy_week=False,
        )
        self.assertEqual(res.verdict, Verdict.INSUFFICIENT)

    def test_low_profit_factor_not_pass(self):
        # positive expectancy, enough trades, but PF < 1.3
        trades = [trade(2) for _ in range(60)] + [trade(-1.6) for _ in range(60)]
        res = uat.evaluate_bucket(
            trades, max_drawdown_limit=10_000, weeks_observed=4,
        )
        # PF = 120 / 96 = 1.25 < 1.3
        self.assertNotEqual(res.verdict, Verdict.PASS)
        self.assertTrue(any("Profit factor" in r for r in res.reasons))

    def test_works_only_in_trends_fails(self):
        # great in trends, clearly negative in chop
        trades = [trade(10, "trending") for _ in range(60)] + [trade(-10, "choppy") for _ in range(60)]
        res = uat.evaluate_bucket(
            trades, max_drawdown_limit=100_000, weeks_observed=4,
        )
        # Overall expectancy is 0 -> killed as fantasy, or fails choppy gate.
        self.assertNotEqual(res.verdict, Verdict.PASS)


if __name__ == "__main__":
    unittest.main()
