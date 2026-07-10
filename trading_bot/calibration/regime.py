"""Regime tagging (UAT addendum Section 3.5).

Tag every period as ``trending`` or ``choppy`` and store the tag on each trade.
Most bots die when the regime shifts, so we need per-regime performance rather
than one blended average.

Two lightweight, dependency-free detectors are provided:

* :func:`ma_slope_regime` — sign-consistency of a moving-average slope. Cheap and
  good enough for a minute/10-minute bot.
* :func:`adx_regime` — a Wilder-style ADX, the classic trend-strength gauge.

Both return the literal ``"trending"`` or ``"choppy"`` so the value drops
straight into the ``regime`` column.
"""

from __future__ import annotations

from typing import List, Literal, Sequence

Regime = Literal["trending", "choppy"]


def moving_average(values: Sequence[float], window: int) -> List[float]:
    """Simple moving average; result is shorter than ``values`` by window-1."""
    if window <= 0:
        raise ValueError("window must be > 0")
    if len(values) < window:
        return []
    out: List[float] = []
    running = sum(values[:window])
    out.append(running / window)
    for i in range(window, len(values)):
        running += values[i] - values[i - window]
        out.append(running / window)
    return out


def ma_slope_regime(
    closes: Sequence[float],
    window: int = 20,
    threshold: float = 0.6,
) -> Regime:
    """Classify by how consistently the moving average moves in one direction.

    We take the MA, look at the sign of each step, and if the dominant direction
    accounts for at least ``threshold`` (default 60%) of the moves, we call it
    trending; otherwise choppy.
    """
    ma = moving_average(closes, window)
    if len(ma) < 2:
        return "choppy"
    ups = downs = 0
    for prev, cur in zip(ma, ma[1:]):
        if cur > prev:
            ups += 1
        elif cur < prev:
            downs += 1
    total = ups + downs
    if total == 0:
        return "choppy"
    dominance = max(ups, downs) / total
    return "trending" if dominance >= threshold else "choppy"


def adx_regime(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
    trend_threshold: float = 25.0,
) -> Regime:
    """Wilder ADX: ADX >= ``trend_threshold`` (classically 25) means trending."""
    adx = compute_adx(highs, lows, closes, period)
    if adx is None:
        return "choppy"
    return "trending" if adx >= trend_threshold else "choppy"


def compute_adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
    """Return the latest ADX value, or ``None`` if there is not enough data.

    Standard Wilder smoothing of +DI / -DI and DX. Needs > 2*period bars.
    """
    n = len(closes)
    if not (len(highs) == len(lows) == n) or n < period * 2 + 1:
        return None

    plus_dm: List[float] = []
    minus_dm: List[float] = []
    tr: List[float] = []
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    def wilder_smooth(seq: List[float]) -> List[float]:
        smoothed = [sum(seq[:period])]
        for i in range(period, len(seq)):
            smoothed.append(smoothed[-1] - smoothed[-1] / period + seq[i])
        return smoothed

    tr_s = wilder_smooth(tr)
    plus_s = wilder_smooth(plus_dm)
    minus_s = wilder_smooth(minus_dm)

    dx: List[float] = []
    for tr_v, p_v, m_v in zip(tr_s, plus_s, minus_s):
        if tr_v == 0:
            dx.append(0.0)
            continue
        plus_di = 100.0 * (p_v / tr_v)
        minus_di = 100.0 * (m_v / tr_v)
        di_sum = plus_di + minus_di
        dx.append(0.0 if di_sum == 0 else 100.0 * abs(plus_di - minus_di) / di_sum)

    if len(dx) < period:
        return None
    # ADX = Wilder-smoothed average of DX.
    adx = sum(dx[:period]) / period
    for i in range(period, len(dx)):
        adx = (adx * (period - 1) + dx[i]) / period
    return adx
