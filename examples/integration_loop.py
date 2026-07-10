"""End-to-end integration demo (Integration Spec §4) — no network, no keys.

Wires the three integrations together and runs the orchestration loop against a
fake broker and a fake Claude client, then closes the position so a full round
trip lands in the calibration layer's raw_pnl / adjusted_pnl tracks.

    signal engine (deterministic TA)  ─┐
    market intelligence (Claude, cached) ├─▶ orchestration loop ─▶ broker (Alpaca port)
    bucket capital + portfolio heat  ─┘                         └─▶ calibration layer

Run: ``python -m examples.integration_loop``
"""

from __future__ import annotations

from trading_bot.broker.accounting import BucketLedger
from trading_bot.broker.base import Bar, BrokerClient, Order, OrderType, Side
from trading_bot.calibration.config import CalibrationConfig
from trading_bot.db.database import CalibrationDB
from trading_bot.intelligence.market_intelligence import (
    ClaudeMarketIntelligence,
    Sentiment,
    VolLevel,
)
from trading_bot.signals.engine import RuleSignalEngine
from trading_bot.orchestration import risk as R
from trading_bot.orchestration.loop import Bucket, TradeRecorder, TradingLoop


class DemoBroker(BrokerClient):
    """A minimal in-memory broker implementing the same port as the Alpaca one."""

    def __init__(self, bars):
        self._bars = bars
        self.orders = []

    def get_bars(self, symbol, timeframe, limit):
        return self._bars.get(symbol, [])

    def stream_bars(self, symbols, on_bar):  # not used in this demo
        raise NotImplementedError

    def get_account(self):
        raise NotImplementedError

    def get_positions(self, bucket_id):
        return []

    def submit_order(self, bucket_id, symbol, side, qty, order_type=OrderType.MARKET,
                     stop_loss=None, take_profit=None):
        o = Order(id=f"o{len(self.orders)}", client_order_id=f"b{bucket_id}_{symbol}_1",
                  symbol=symbol, side=side, qty=qty, order_type=order_type,
                  status="filled", filled_qty=qty, bucket_id=bucket_id)
        self.orders.append(o)
        print(f"  ORDER  bucket={bucket_id} {side.value} {qty:.0f} {symbol} "
              f"sl={stop_loss:.2f} tp={take_profit:.2f}")
        return o

    def close_position(self, bucket_id, symbol):
        print(f"  CLOSE  bucket={bucket_id} {symbol}")


class FakeClaude:
    """Stands in for the Anthropic SDK — returns strict JSON sentiment."""

    def complete(self, system, user):
        return '{"sentiment":"bullish","volatility":"low","perSymbol":{"AAPL":"bullish"},"notes":"demo"}'


def accelerating_uptrend(symbol, n=60):
    return [
        Bar(symbol=symbol, timestamp_ms=i * 60_000, open=100 + 0.05 * i * i,
            high=100 + 0.05 * i * i + 0.5, low=100 + 0.05 * i * i - 0.5,
            close=100 + 0.05 * i * i, volume=(50_000 if i == n - 1 else 40_000))
        for i in range(n)
    ]


def run() -> None:
    bars = {"AAPL": accelerating_uptrend("AAPL")}
    broker = DemoBroker(bars)

    ledger = BucketLedger(total_capital=100_000, portfolio_heat_cap_pct=0.10)
    ledger.register_bucket(1, capital_ceiling=25_000)

    db = CalibrationDB(":memory:")
    db.add_bucket("momentum", risk_tier="high", max_drawdown=2_000)

    config = CalibrationConfig(impact_k=0.15, liquidity_cap_pct=0.05, latency_ms=400)
    recorder = TradeRecorder(db, config)

    intelligence = ClaudeMarketIntelligence(
        FakeClaude(), news_fetch=lambda syms: [], refresh_seconds=600, clock=lambda: 0,
    )

    loop = TradingLoop(broker, RuleSignalEngine(), intelligence, ledger, recorder,
                       risk_config=R.RiskConfig(base_position_notional=5_000))

    bucket = Bucket(id=1, watchlist=["AAPL"], capital_ceiling=25_000)

    print("=== TICK: run loop for bucket 1 ===")
    ctx = intelligence.assess(bucket.watchlist)
    print(f"MarketContext: sentiment={ctx.sentiment.value} vol={ctx.volatility.value} "
          f"(1 Claude call, then cached for all buckets)")
    result = loop.run_bucket_once(bucket)
    print(f"submitted={result.submitted} skipped={result.skipped} reasons={result.reasons}")
    print(f"bucket 1 capital used=${ledger.get(1).used:,.0f} / ${ledger.get(1).capital_ceiling:,.0f} "
          f"| portfolio heat={ledger.portfolio_heat():.1%}")

    print("\n=== EXIT: close the position -> round trip into calibration ===")
    exit_bar = Bar("AAPL", 3_600_000, 290, 290.5, 289.5, 290.0, 40_000)
    broker.close_position(1, "AAPL")
    trade_id = recorder.record_exit(1, "AAPL", exit_bar)

    row = db.trades_for_bucket(1)[0]
    print(f"trade #{trade_id}: raw_pnl=${row['raw_pnl']:.2f}  "
          f"adjusted_pnl=${row['adjusted_pnl']:.2f}  "
          f"friction_removed=${row['raw_pnl'] - row['adjusted_pnl']:.2f}  "
          f"regime={row['regime']}")
    print("\nadjusted_pnl is what forecasts/UAT use — the honest predictor of live.")
    db.close()


if __name__ == "__main__":
    run()
