"""Unit tests for the friction math (UAT addendum Section 3)."""

import math
import unittest

from trading_bot.calibration.config import CalibrationConfig, FeeSchedule
from trading_bot.calibration import friction as F


class TestHalfSpread(unittest.TestCase):
    def setUp(self):
        self.cfg = CalibrationConfig(half_spread_min=0.01)

    def test_uses_half_of_spread_when_above_floor(self):
        self.assertEqual(F.half_spread(0.10, self.cfg), 0.05)

    def test_enforces_minimum_floor(self):
        # spread/2 = 0.005 < floor 0.01 -> floor wins
        self.assertEqual(F.half_spread(0.01, self.cfg), 0.01)

    def test_zero_spread_returns_floor(self):
        self.assertEqual(F.half_spread(0.0, self.cfg), 0.01)

    def test_negative_spread_rejected(self):
        with self.assertRaises(ValueError):
            F.half_spread(-1.0, self.cfg)


class TestMarketImpact(unittest.TestCase):
    def setUp(self):
        self.cfg = CalibrationConfig(impact_k=0.2)

    def test_scales_with_participation(self):
        # k * (qty / volume) = 0.2 * (100 / 1000) = 0.02
        self.assertAlmostEqual(F.market_impact(100, 1000, self.cfg), 0.02)

    def test_bigger_order_more_impact(self):
        small = F.market_impact(50, 1000, self.cfg)
        big = F.market_impact(500, 1000, self.cfg)
        self.assertGreater(big, small)

    def test_zero_volume_returns_zero_impact(self):
        # Liquidity cap handles a zero-volume bar; impact stays 0 here.
        self.assertEqual(F.market_impact(100, 0, self.cfg), 0.0)


class TestSlippagePerShare(unittest.TestCase):
    def test_is_half_spread_plus_impact(self):
        cfg = CalibrationConfig(impact_k=0.1, half_spread_min=0.0)
        # half_spread = 0.04/2 = 0.02 ; impact = 0.1 * 10/1000 = 0.001
        slip = F.slippage_per_share(10, 0.04, 1000, cfg)
        self.assertAlmostEqual(slip, 0.021)


class TestApplySlippage(unittest.TestCase):
    def test_buy_fills_higher(self):
        self.assertAlmostEqual(F.apply_slippage(100.0, "buy", 0.05), 100.05)

    def test_sell_fills_lower(self):
        self.assertAlmostEqual(F.apply_slippage(100.0, "sell", 0.05), 99.95)

    def test_sell_never_goes_negative(self):
        self.assertEqual(F.apply_slippage(0.01, "sell", 1.0), 0.0)

    def test_unknown_side(self):
        with self.assertRaises(ValueError):
            F.apply_slippage(100.0, "hold", 0.05)


class TestLiquidityCap(unittest.TestCase):
    def setUp(self):
        self.cfg = CalibrationConfig(liquidity_cap_pct=0.02)  # 2% of bar volume

    def test_small_order_fully_filled(self):
        res = F.apply_liquidity_cap(10, 10000, self.cfg)  # cap = 200
        self.assertEqual(res.filled_qty, 10)
        self.assertEqual(res.haircut_qty, 0.0)

    def test_large_order_haircut_to_cap(self):
        res = F.apply_liquidity_cap(500, 10000, self.cfg)  # cap = 200
        self.assertEqual(res.filled_qty, 200)
        self.assertEqual(res.haircut_qty, 300)

    def test_exact_cap_not_haircut(self):
        res = F.apply_liquidity_cap(200, 10000, self.cfg)
        self.assertEqual(res.filled_qty, 200)
        self.assertEqual(res.haircut_qty, 0.0)

    def test_zero_volume_rejects_everything(self):
        res = F.apply_liquidity_cap(100, 0, self.cfg)
        self.assertEqual(res.filled_qty, 0.0)
        self.assertEqual(res.haircut_qty, 100)


class TestLatency(unittest.TestCase):
    def setUp(self):
        self.cfg = CalibrationConfig(latency_ms=400)
        # ticks at 0,200,400,600 ms
        self.series = [(0, 100.0), (200, 100.5), (400, 101.0), (600, 101.5)]

    def test_price_at_exact_tick(self):
        self.assertEqual(F.price_at(self.series, 400), 101.0)

    def test_price_at_rounds_up_to_next_tick(self):
        self.assertEqual(F.price_at(self.series, 300), 101.0)

    def test_price_after_last_tick_is_none(self):
        self.assertIsNone(F.price_at(self.series, 10_000))

    def test_latency_reprices_at_delayed_timestamp(self):
        # signal at t=0, +400ms latency -> tick at 400 -> 101.0
        price = F.latency_adjusted_price(0, 100.0, self.series, self.cfg)
        self.assertEqual(price, 101.0)

    def test_no_series_falls_back_to_signal_price(self):
        self.assertEqual(F.latency_adjusted_price(0, 100.0, None, self.cfg), 100.0)

    def test_beyond_series_falls_back_to_signal(self):
        price = F.latency_adjusted_price(10_000, 100.0, self.series, self.cfg)
        self.assertEqual(price, 100.0)


class TestFees(unittest.TestCase):
    def test_sell_fees_sec_plus_taf(self):
        fees = FeeSchedule(
            sec_fee_per_dollar=0.0000278,
            finra_taf_per_share=0.000166,
            finra_taf_max=8.30,
        )
        # 1000 shares @ $50 = $50,000 proceeds
        sec = 0.0000278 * 50_000       # 1.39
        taf = 0.000166 * 1000          # 0.166
        self.assertAlmostEqual(fees.sell_fees(50_000, 1000), sec + taf)

    def test_taf_is_capped(self):
        fees = FeeSchedule(finra_taf_per_share=0.001, finra_taf_max=8.30, sec_fee_per_dollar=0)
        # 100k shares * 0.001 = 100 -> capped to 8.30
        self.assertAlmostEqual(fees.sell_fees(1_000_000, 100_000), 8.30)

    def test_no_fees_on_zero(self):
        self.assertEqual(FeeSchedule().sell_fees(0, 0), 0.0)


class TestModelFill(unittest.TestCase):
    def setUp(self):
        self.cfg = CalibrationConfig(
            impact_k=0.1, liquidity_cap_pct=0.5, latency_ms=0, half_spread_min=0.0
        )

    def test_buy_fill_incorporates_slippage(self):
        f = F.model_fill(
            side="buy", order_qty=100, signal_time_ms=0, signal_price=50.0,
            spread=0.10, bar_volume=1000, config=self.cfg,
        )
        # half_spread 0.05 + impact 0.1*100/1000=0.01 = 0.06 -> 50.06
        self.assertAlmostEqual(f.modeled_slippage, 0.06)
        self.assertAlmostEqual(f.modeled_fill_price, 50.06)
        self.assertEqual(f.filled_qty, 100)
        self.assertEqual(f.haircut_qty, 0.0)

    def test_haircut_flows_through(self):
        cfg = self.cfg.with_updates(liquidity_cap_pct=0.02)  # cap = 20
        f = F.model_fill(
            side="buy", order_qty=100, signal_time_ms=0, signal_price=50.0,
            spread=0.10, bar_volume=1000, config=cfg,
        )
        self.assertEqual(f.filled_qty, 20)
        self.assertEqual(f.haircut_qty, 80)


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        # No latency, generous cap; isolate slippage + fees.
        self.cfg = CalibrationConfig(
            impact_k=0.0, liquidity_cap_pct=1.0, latency_ms=0, half_spread_min=0.01,
            fees=FeeSchedule(sec_fee_per_dollar=0.0, finra_taf_per_share=0.0, finra_taf_max=0.0),
        )

    def _fill(self, side, qty, price):
        return F.model_fill(
            side=side, order_qty=qty, signal_time_ms=0, signal_price=price,
            spread=0.02, bar_volume=1_000_000, config=self.cfg,
        )

    def test_long_raw_vs_adjusted(self):
        entry = self._fill("buy", 100, 50.0)   # fills at 50.01
        exit = self._fill("sell", 100, 51.0)   # fills at 50.99
        tr = F.model_round_trip(entry, exit, self.cfg, direction="long")
        # raw uses signal prices: (51 - 50) * 100 = 100
        self.assertAlmostEqual(tr.raw_pnl, 100.0)
        # adjusted: (50.99 - 50.01) * 100 = 98
        self.assertAlmostEqual(tr.adjusted_pnl, 98.0)
        # friction removed 2.0 of fake edge
        self.assertAlmostEqual(tr.pnl_gap, 2.0)

    def test_adjusted_always_worse_than_raw_for_winner(self):
        entry = self._fill("buy", 100, 50.0)
        exit = self._fill("sell", 100, 51.0)
        tr = F.model_round_trip(entry, exit, self.cfg)
        self.assertLess(tr.adjusted_pnl, tr.raw_pnl)

    def test_fees_reduce_adjusted_pnl(self):
        cfg = self.cfg.with_updates(
            fees=FeeSchedule(sec_fee_per_dollar=0.001, finra_taf_per_share=0.0, finra_taf_max=0.0)
        )
        entry = self._fill("buy", 100, 50.0)
        exit = self._fill("sell", 100, 51.0)
        tr = F.model_round_trip(entry, exit, cfg)
        # sell proceeds = 50.99 * 100 = 5099 ; fee = 5.099
        self.assertAlmostEqual(tr.fees, 5.099, places=3)
        self.assertAlmostEqual(tr.adjusted_pnl, 98.0 - 5.099, places=3)

    def test_short_direction(self):
        # Short: open by selling high, close by buying low.
        entry = self._fill("sell", 100, 51.0)  # opening sell fills at 50.99
        exit = self._fill("buy", 100, 50.0)    # closing buy fills at 50.01
        tr = F.model_round_trip(entry, exit, self.cfg, direction="short")
        # raw: (51 - 50) * 100 = 100
        self.assertAlmostEqual(tr.raw_pnl, 100.0)
        # adjusted: (50.99 - 50.01) * 100 = 98
        self.assertAlmostEqual(tr.adjusted_pnl, 98.0)

    def test_qty_is_min_of_legs(self):
        entry = self._fill("buy", 100, 50.0)
        exit = self._fill("sell", 60, 51.0)
        tr = F.model_round_trip(entry, exit, self.cfg)
        # can't close more than opened -> qty = 60
        self.assertAlmostEqual(tr.raw_pnl, 60.0)


class TestConfigValidation(unittest.TestCase):
    def test_bad_liquidity_cap(self):
        with self.assertRaises(ValueError):
            CalibrationConfig(liquidity_cap_pct=0)
        with self.assertRaises(ValueError):
            CalibrationConfig(liquidity_cap_pct=1.5)

    def test_negative_latency(self):
        with self.assertRaises(ValueError):
            CalibrationConfig(latency_ms=-1)

    def test_with_updates_is_immutable_copy(self):
        base = CalibrationConfig(impact_k=0.1)
        updated = base.with_updates(impact_k=0.3)
        self.assertEqual(base.impact_k, 0.1)
        self.assertEqual(updated.impact_k, 0.3)


if __name__ == "__main__":
    unittest.main()
