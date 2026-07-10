"""Unit tests for the orchestration loop (Integration Spec §4).

Drives the full loop with fakes: signal gating, sizing, per-bucket capital,
portfolio heat, daily-loss pause, and fills flowing into the calibration layer.
"""

import unittest

from trading_bot.broker.accounting import BucketLedger
from trading_bot.broker.base import Bar, BrokerClient, Order, OrderType, Side
from trading_bot.calibration.config import CalibrationConfig, FeeSchedule
from trading_bot.db.database import CalibrationDB
from trading_bot.intelligence.market_intelligence import MarketContext, Sentiment, VolLevel
from trading_bot.signals.engine import Signal, SignalEngine
from trading_bot.orchestration import risk as R
from trading_bot.orchestration.loop import Bucket, TradeRecorder, TradingLoop


# --- fakes -----------------------------------------------------------------
def make_bars(symbol, n=60, start=100.0, vol=10_000.0):
    return [
        Bar(symbol=symbol, timestamp_ms=i * 60_000, open=start + i, high=start + i + 0.5,
            low=start + i - 0.5, close=start + i, volume=vol)
        for i in range(n)
    ]


class FakeBroker(BrokerClient):
    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol
        self.orders = []

    def get_bars(self, symbol, timeframe, limit):
        return self.bars_by_symbol.get(symbol, [])

    def stream_bars(self, symbols, on_bar):
        raise NotImplementedError

    def get_account(self):
        raise NotImplementedError

    def get_positions(self, bucket_id):
        return []

    def submit_order(self, bucket_id, symbol, side, qty, order_type=OrderType.MARKET,
                     stop_loss=None, take_profit=None):
        order = Order(id=f"o{len(self.orders)}", client_order_id=f"b{bucket_id}_{symbol}_1",
                      symbol=symbol, side=side, qty=qty, order_type=order_type,
                      status="new", bucket_id=bucket_id)
        self.orders.append(order)
        return order

    def close_position(self, bucket_id, symbol):
        pass


class FixedSignal(SignalEngine):
    def __init__(self, signal):
        self.signal = signal

    def evaluate(self, symbol, bars):
        return self.signal


class FixedIntel:
    def __init__(self, ctx):
        self.ctx = ctx

    def assess(self, symbols):
        return self.ctx


def neutral_ctx():
    return MarketContext(Sentiment.NEUTRAL, VolLevel.MEDIUM, {}, "", 0)


def build_loop(signal, ctx=None, capital_ceiling=1_000_000, risk_config=None):
    broker = FakeBroker({"AAPL": make_bars("AAPL")})
    ledger = BucketLedger(total_capital=1_000_000)
    ledger.register_bucket(1, capital_ceiling=capital_ceiling)
    db = CalibrationDB(":memory:")
    db.add_bucket("test")
    config = CalibrationConfig(liquidity_cap_pct=1.0, latency_ms=0,
                              fees=FeeSchedule(0, 0, 0))
    recorder = TradeRecorder(db, config)
    loop = TradingLoop(broker, FixedSignal(signal), FixedIntel(ctx or neutral_ctx()),
                       ledger, recorder, risk_config=risk_config or R.RiskConfig())
    return loop, broker, ledger, db, recorder


class TestLoopGating(unittest.TestCase):
    def test_no_signal_skips(self):
        loop, broker, *_ = build_loop(signal=None)
        res = loop.run_bucket_once(Bucket(1, ["AAPL"], 1_000_000))
        self.assertEqual(res.submitted, 0)
        self.assertEqual(broker.orders, [])
        self.assertIn("no_signal", res.reasons)

    def test_strong_signal_submits(self):
        sig = Signal("AAPL", Side.BUY, 0.9, "strong")
        loop, broker, ledger, *_ = build_loop(sig)
        res = loop.run_bucket_once(Bucket(1, ["AAPL"], 1_000_000))
        self.assertEqual(res.submitted, 1)
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(broker.orders[0].side, Side.BUY)
        # bracket stops were passed (capital deployed)
        self.assertGreater(ledger.get(1).used, 0)

    def test_low_confidence_skipped(self):
        sig = Signal("AAPL", Side.BUY, 0.2, "weak")
        loop, broker, *_ = build_loop(sig)
        res = loop.run_bucket_once(Bucket(1, ["AAPL"], 1_000_000))
        self.assertEqual(res.submitted, 0)
        self.assertIn("risk_gate", res.reasons)

    def test_high_vol_raises_confidence_bar(self):
        sig = Signal("AAPL", Side.BUY, 0.6, "medium")
        ctx = MarketContext(Sentiment.NEUTRAL, VolLevel.HIGH, {}, "", 0)
        loop, broker, *_ = build_loop(sig, ctx=ctx)
        res = loop.run_bucket_once(Bucket(1, ["AAPL"], 1_000_000))
        # 0.6 < high_vol_confidence 0.7 -> skipped
        self.assertEqual(res.submitted, 0)


class TestCapitalGuards(unittest.TestCase):
    def test_bucket_capital_ceiling_blocks(self):
        sig = Signal("AAPL", Side.BUY, 0.9, "strong")
        # Tiny ceiling: base_notional*conf = 900 but ceiling only 10 -> blocked
        loop, broker, *_ = build_loop(sig, capital_ceiling=10)
        res = loop.run_bucket_once(Bucket(1, ["AAPL"], 10))
        self.assertEqual(res.submitted, 0)
        self.assertIn("bucket_capital", res.reasons)

    def test_portfolio_heat_blocks(self):
        sig = Signal("AAPL", Side.BUY, 0.9, "strong")
        loop, broker, ledger, *_ = build_loop(sig)
        # Pre-load heat near the cap so the new trade's risk tips it over.
        ledger.get(1).open_risk = 99_999
        res = loop.run_bucket_once(Bucket(1, ["AAPL"], 1_000_000))
        self.assertEqual(res.submitted, 0)
        self.assertIn("portfolio_heat", res.reasons)


class TestDailyLossPause(unittest.TestCase):
    def test_daily_loss_pauses_only_this_bucket(self):
        sig = Signal("AAPL", Side.BUY, 0.9, "strong")
        loop, broker, *_ = build_loop(sig)
        loop.record_realized(1, -600)  # exceeds default 500 daily limit
        res = loop.run_bucket_once(Bucket(1, ["AAPL"], 1_000_000))
        self.assertEqual(res.submitted, 0)
        self.assertIn("bucket_paused", res.reasons)
        self.assertTrue(loop.runtime(1).paused)
        # A different bucket is unaffected
        self.assertFalse(loop.runtime(2).paused)


class TestCalibrationIntegration(unittest.TestCase):
    def test_fill_feeds_raw_and_adjusted_pnl(self):
        sig = Signal("AAPL", Side.BUY, 0.9, "strong")
        loop, broker, ledger, db, recorder = build_loop(sig)
        loop.run_bucket_once(Bucket(1, ["AAPL"], 1_000_000))
        self.assertTrue(recorder.has_open(1, "AAPL"))

        # Close the position at a higher bar -> round trip persisted.
        exit_bar = Bar("AAPL", 999_000, 170, 170.5, 169.5, 170.0, 10_000)
        trade_id = recorder.record_exit(1, "AAPL", exit_bar)
        self.assertIsNotNone(trade_id)

        rows = db.trades_for_bucket(1)
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["raw_pnl"])
        self.assertIsNotNone(rows[0]["adjusted_pnl"])
        # Friction makes adjusted worse than raw for a winner
        self.assertLess(rows[0]["adjusted_pnl"], rows[0]["raw_pnl"])
        self.assertIn(rows[0]["regime"], ("trending", "choppy"))


if __name__ == "__main__":
    unittest.main()
