"""Backtest against REAL Alpaca paper (IEX) data.

Run this where Alpaca is reachable (your machine, or an environment with
unrestricted egress). Credentials are read from environment variables — never
hardcoded, never committed:

    export APCA_API_KEY_ID=PK...
    export APCA_API_SECRET_KEY=...
    python -m examples.backtest_alpaca AAPL MSFT NVDA

Fetches historical 1-minute IEX bars for each symbol, runs the deterministic
signal engine through the friction model, and prints the UAT verdict on
adjusted_pnl. Free Basic/IEX data is all a paper account gets — which is exactly
the honest baseline the calibration layer is built around.
"""

from __future__ import annotations

import os
import sys

from trading_bot.backtest.engine import Backtester, BacktestConfig
from trading_bot.broker.alpaca import AlpacaBrokerClient, AlpacaError
from trading_bot.calibration.config import CalibrationConfig
from trading_bot.signals.engine import RuleSignalEngine


def keystore_from_env():
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in the environment.")
    return lambda: {"api_key_id": key, "api_secret_key": secret}


def main(argv) -> None:
    symbols = argv or ["AAPL", "MSFT", "NVDA"]
    bars_wanted = int(os.environ.get("BARS", "5000"))

    broker = AlpacaBrokerClient(keystore_from_env(), paper=True)

    # Confirm auth first — a clear message beats a wall of stack trace.
    try:
        acct = broker.get_account()
        print(f"Connected: equity=${acct.equity:,.2f} buying_power=${acct.buying_power:,.2f}\n")
    except AlpacaError as e:
        sys.exit(f"Alpaca auth/connectivity failed: {e}")

    universe = {}
    for sym in symbols:
        try:
            bars = broker.get_bars(sym, "1Min", bars_wanted)
            print(f"  {sym}: {len(bars)} bars")
            if bars:
                universe[sym] = bars
        except AlpacaError as e:
            print(f"  {sym}: fetch failed — {e}")

    if not universe:
        sys.exit("No bars fetched; nothing to backtest.")

    calib = CalibrationConfig(impact_k=0.15, liquidity_cap_pct=0.05, latency_ms=400)
    bt = Backtester(RuleSignalEngine(), calib, BacktestConfig(warmup_bars=60))
    result = bt.run(universe, trades_per_day=10)

    print("\n=== BACKTEST ON REAL IEX DATA (adjusted_pnl is the number) ===\n")
    print(result.summary())
    if result.forecast:
        f = result.forecast
        print(f"\nforecast: weekly={f['weekly']:.0f}  monthly={f['monthly']:.0f}  "
              f"yearly={f['yearly']:.0f}")


if __name__ == "__main__":
    main(sys.argv[1:])
