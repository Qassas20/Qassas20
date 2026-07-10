"""Backtest the RuleSignalEngine over synthetic history and print the verdict.

Run: ``python -m examples.run_backtest``

Swap ``synthetic_bars`` for real Alpaca historical bars (via
``AlpacaBrokerClient.get_bars``) to backtest against real IEX data. The numbers
that matter are on adjusted_pnl, split by regime — the UAT criteria.
"""

from __future__ import annotations

from trading_bot.backtest.data import synthetic_bars
from trading_bot.backtest.engine import Backtester, BacktestConfig
from trading_bot.calibration.config import CalibrationConfig
from trading_bot.signals.engine import RuleSignalEngine


def run() -> None:
    calib = CalibrationConfig(impact_k=0.15, liquidity_cap_pct=0.05, latency_ms=400)
    bt_config = BacktestConfig(warmup_bars=60, base_notional=5_000,
                               stop_loss_pct=0.02, take_profit_pct=0.04)

    # A small synthetic universe with different seeds/drifts.
    universe = {
        "AAA": synthetic_bars("AAA", n=2000, seed=1, drift=0.0003),
        "BBB": synthetic_bars("BBB", n=2000, seed=2, drift=0.0),
        "CCC": synthetic_bars("CCC", n=2000, seed=3, drift=-0.0002),
    }

    backtester = Backtester(RuleSignalEngine(), calib, bt_config)
    result = backtester.run(universe, trades_per_day=10)

    print("=== BACKTEST (adjusted_pnl is the number that matters) ===\n")
    print(result.summary())
    if result.forecast:
        f = result.forecast
        print(f"\nforecast (from adjusted expectancy): "
              f"weekly={f['weekly']:.0f}  monthly={f['monthly']:.0f}  yearly={f['yearly']:.0f}")
    print("\nNote: synthetic random-walk data — this exercises the harness, not a real edge. "
          "Feed real Alpaca bars to measure the actual strategy.")


if __name__ == "__main__":
    run()
