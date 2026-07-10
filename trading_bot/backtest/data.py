"""Synthetic historical bars for backtests and tests.

A deterministic random-walk-with-drift OHLCV generator (seeded, reproducible).
Real backtests should feed *actual* Alpaca historical bars instead — this exists
so the harness and its tests run with no data dependency.
"""

from __future__ import annotations

import random
from typing import List

from ..broker.base import Bar


def synthetic_bars(
    symbol: str,
    n: int = 500,
    seed: int = 0,
    start_price: float = 100.0,
    drift: float = 0.0002,        # per-bar log drift
    volatility: float = 0.004,    # per-bar log stdev
    base_volume: float = 40_000.0,
    start_ms: int = 0,
    step_ms: int = 60_000,        # 1-minute bars
) -> List[Bar]:
    """Generate ``n`` reproducible OHLCV bars via geometric random walk."""
    rng = random.Random(seed)
    bars: List[Bar] = []
    price = start_price
    for i in range(n):
        ret = drift + volatility * rng.gauss(0, 1)
        close = max(price * (1 + ret), 0.01)
        high = max(price, close) * (1 + abs(volatility * rng.gauss(0, 1)))
        low = min(price, close) * (1 - abs(volatility * rng.gauss(0, 1)))
        volume = base_volume * (0.5 + rng.random())
        bars.append(Bar(
            symbol=symbol, timestamp_ms=start_ms + i * step_ms,
            open=price, high=high, low=low, close=close, volume=volume,
        ))
        price = close
    return bars


def trending_bars(symbol: str, n: int = 500, seed: int = 0, up: bool = True,
                  **kwargs) -> List[Bar]:
    """A series with a clear drift (for exercising trending-regime paths)."""
    drift = 0.001 if up else -0.001
    return synthetic_bars(symbol, n=n, seed=seed, drift=drift, **kwargs)
