"""Signal engine — deterministic rule composition (Integration Spec §2).

Input: bars from the broker. Output: a :class:`Signal` or ``None`` (no trade).
This is exactly what the backtest runs against — feed known bars, assert the
expected signal. No LLM anywhere in this path.

``RuleSignalEngine`` emits a signal only when multiple conditions align, and
sets ``confidence`` from how many agree (so the risk layer can gate weak setups).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..broker.base import Bar, Side
from . import indicators as ind


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    confidence: float   # 0.0 - 1.0, from how many conditions agree
    reason: str


class SignalEngine(ABC):
    @abstractmethod
    def evaluate(self, symbol: str, bars: Sequence[Bar]) -> Optional[Signal]:
        """Return a Signal, or None for no trade."""


@dataclass(frozen=True)
class RuleConfig:
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    ema_fast: int = 9
    ema_slow: int = 21
    volume_lookback: int = 20
    volume_factor: float = 1.2
    min_conditions: int = 2   # need at least this many aligned to emit


class RuleSignalEngine(SignalEngine):
    """EMA cross + RSI + MACD + volume confirmation.

    A BUY needs bullish conditions to align (EMA fast>slow / bullish cross,
    RSI leaving oversold, MACD bull cross); a SELL is the mirror. Confidence is
    ``agreeing / total_checked``.
    """

    def __init__(self, config: RuleConfig = RuleConfig()) -> None:
        self.config = config

    def evaluate(self, symbol: str, bars: Sequence[Bar]) -> Optional[Signal]:
        c = self.config
        closes = [b.close for b in bars]
        volumes = [b.volume for b in bars]
        # Need enough history for the slow EMA + MACD stack.
        if len(closes) < max(c.ema_slow, 26) + c.rsi_period:
            return None

        ema_fast = ind.ema(closes, c.ema_fast)
        ema_slow = ind.ema(closes, c.ema_slow)
        rsi_vals = ind.rsi(closes, c.rsi_period)
        macd_res = ind.macd(closes)
        if not (ema_fast and ema_slow and rsi_vals and macd_res.histogram):
            return None

        bull_votes: List[str] = []
        bear_votes: List[str] = []
        total = 0

        # 1) EMA trend + cross
        total += 1
        offset = len(ema_fast) - len(ema_slow)
        f_now, f_prev = ema_fast[-1], ema_fast[-2]
        s_now, s_prev = ema_slow[-1], ema_slow[-2]
        if f_now > s_now:
            bull_votes.append("EMA fast>slow")
            if f_prev <= s_prev:
                bull_votes.append("EMA bull cross")
                total += 1
        elif f_now < s_now:
            bear_votes.append("EMA fast<slow")
            if f_prev >= s_prev:
                bear_votes.append("EMA bear cross")
                total += 1

        # 2) RSI
        total += 1
        rsi_now = rsi_vals[-1]
        if rsi_now <= c.rsi_oversold:
            bull_votes.append("RSI oversold")
        elif rsi_now >= c.rsi_overbought:
            bear_votes.append("RSI overbought")

        # 3) MACD momentum (histogram sign + flip)
        total += 1
        h_now, h_prev = macd_res.histogram[-1], macd_res.histogram[-2]
        if h_now > 0:
            bull_votes.append("MACD bull")
            if h_prev <= 0:
                bull_votes.append("MACD bull cross")
        elif h_now < 0:
            bear_votes.append("MACD bear")
            if h_prev >= 0:
                bear_votes.append("MACD bear cross")

        # 4) Volume confirmation (adds weight to whichever side leads)
        total += 1
        vol_ok = ind.volume_confirms(volumes, c.volume_lookback, c.volume_factor)
        if vol_ok:
            if len(bull_votes) >= len(bear_votes) and bull_votes:
                bull_votes.append("volume confirm")
            elif bear_votes:
                bear_votes.append("volume confirm")

        # Decide side; require multiple *distinct condition groups* to align.
        bull_groups = _distinct_groups(bull_votes)
        bear_groups = _distinct_groups(bear_votes)

        if bull_groups >= c.min_conditions and bull_groups > bear_groups:
            return Signal(symbol, Side.BUY, min(bull_groups / total, 1.0),
                          " + ".join(bull_votes))
        if bear_groups >= c.min_conditions and bear_groups > bull_groups:
            return Signal(symbol, Side.SELL, min(bear_groups / total, 1.0),
                          " + ".join(bear_votes))
        return None


def _distinct_groups(votes: Sequence[str]) -> int:
    """Count distinct indicator groups (EMA / RSI / MACD / volume) that voted."""
    groups = set()
    for v in votes:
        groups.add(v.split()[0])  # "EMA", "RSI", "MACD", "volume"
    return len(groups)
