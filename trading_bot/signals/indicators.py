"""Technical indicators — deterministic, dependency-free (Integration Spec §2).

These are pure math over price/volume series. NOT an LLM call: building the TA
engine as a Claude call would be slow, expensive, non-deterministic, and
un-backtestable. Everything here is exact and unit-tested against known bars.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence


def ema(values: Sequence[float], period: int) -> List[float]:
    """Exponential moving average. Result is aligned to the tail of ``values``.

    Seeds with the SMA of the first ``period`` points (Wilder-style seed), then
    applies the standard multiplier ``2/(period+1)``.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append((v - out[-1]) * k + out[-1])
    return out


def rsi(closes: Sequence[float], period: int = 14) -> List[float]:
    """Wilder's RSI. Returns one value per bar after the first ``period`` bars."""
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(closes) <= period:
        return []
    gains, losses = [], []
    for prev, cur in zip(closes, closes[1:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [_rsi_value(avg_gain, avg_loss)]
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(_rsi_value(avg_gain, avg_loss))
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass(frozen=True)
class MacdResult:
    macd: List[float]      # MACD line (fast EMA - slow EMA)
    signal: List[float]    # EMA of the MACD line
    histogram: List[float] # macd - signal
    # All three are aligned to each other (same length, same tail).


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> MacdResult:
    """MACD(12,26,9). Empty result if not enough bars for the full stack."""
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    if not fast_ema or not slow_ema:
        return MacdResult([], [], [])
    # slow EMA starts later; align both to the slow EMA's start.
    offset = len(fast_ema) - len(slow_ema)
    fast_aligned = fast_ema[offset:]
    macd_line = [f - s for f, s in zip(fast_aligned, slow_ema)]
    signal_line = ema(macd_line, signal)
    if not signal_line:
        return MacdResult(macd_line, [], [])
    macd_tail = macd_line[len(macd_line) - len(signal_line):]
    histogram = [m - s for m, s in zip(macd_tail, signal_line)]
    return MacdResult(macd_tail, signal_line, histogram)


def volume_confirms(volumes: Sequence[float], lookback: int = 20, factor: float = 1.2) -> bool:
    """True if the latest bar's volume exceeds ``factor``× the recent average.

    Breakouts need volume or they're suspect.
    """
    if len(volumes) < lookback + 1:
        return False
    recent_avg = sum(volumes[-lookback - 1:-1]) / lookback
    if recent_avg == 0:
        return False
    return volumes[-1] >= factor * recent_avg


@dataclass(frozen=True)
class SupportResistance:
    support: Optional[float]
    resistance: Optional[float]


def support_resistance(bars_high: Sequence[float], bars_low: Sequence[float],
                       lookback: int = 20) -> SupportResistance:
    """Naive S/R: the recent swing low / swing high over ``lookback`` bars."""
    if not bars_high or not bars_low:
        return SupportResistance(None, None)
    window_high = bars_high[-lookback:]
    window_low = bars_low[-lookback:]
    return SupportResistance(support=min(window_low), resistance=max(window_high))
