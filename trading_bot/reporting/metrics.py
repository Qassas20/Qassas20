"""Reports-tab metrics (UAT addendum Section 4).

Every performance metric is computed from ``adjusted_pnl`` — never ``raw_pnl`` —
because paper's raw numbers are an optimistic mirage. The reality-gap metrics are
the ones unique to a *valid* UAT: they quantify exactly how much fake edge the
friction layer removed.

Input is a list of trade-like mappings (``sqlite3.Row`` or plain dicts) that
expose at least ``adjusted_pnl`` / ``raw_pnl`` and the reality-gap fields.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence


def _get(trade: Mapping, key: str, default: float = 0.0) -> float:
    """Read ``key`` from a sqlite3.Row or a plain dict, mapping NULL -> default."""
    try:
        keys = trade.keys()  # both dict and sqlite3.Row expose keys()
    except AttributeError:
        keys = ()
    val = trade[key] if key in keys else default
    return default if val is None else val


def _adjusted(trade: Mapping) -> float:
    return _get(trade, "adjusted_pnl")


@dataclass
class CoreMetrics:
    trade_count: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0            # reported as a positive magnitude
    expectancy: float = 0.0         # per-trade expected adjusted P&L
    profit_factor: float = 0.0      # gross profit / gross loss
    gross_profit: float = 0.0
    gross_loss: float = 0.0         # positive magnitude
    max_drawdown: float = 0.0       # positive magnitude, on the equity curve
    sharpe: float = 0.0
    sortino: float = 0.0
    total_adjusted_pnl: float = 0.0


@dataclass
class RealityGapMetrics:
    modeled_slippage_per_trade: float = 0.0
    cumulative_slippage_cost: float = 0.0
    total_fees: float = 0.0
    avg_fill_drift_bps: float = 0.0     # signal price -> modeled fill, in bps
    haircut_qty_total: float = 0.0      # fake fill removed by liquidity cap
    raw_vs_adjusted_gap: float = 0.0    # sum(raw) - sum(adjusted)
    total_raw_pnl: float = 0.0
    total_adjusted_pnl: float = 0.0


@dataclass
class RegimeSplit:
    trending: CoreMetrics = field(default_factory=CoreMetrics)
    choppy: CoreMetrics = field(default_factory=CoreMetrics)


# ---------------------------------------------------------------------------
# Core performance
# ---------------------------------------------------------------------------
def expectancy(trades: Sequence[Mapping]) -> float:
    """(win% * avg_win) - (loss% * avg_loss). The number that matters.

    Equivalent to the mean adjusted P&L per trade, which is how it's computed.
    """
    if not trades:
        return 0.0
    return sum(_adjusted(t) for t in trades) / len(trades)


def profit_factor(trades: Sequence[Mapping]) -> float:
    """Gross profit / gross loss (target > 1.3).

    ``inf`` if there are wins but no losses; ``0`` if there is no profit.
    """
    gross_profit = sum(_adjusted(t) for t in trades if _adjusted(t) > 0)
    gross_loss = -sum(_adjusted(t) for t in trades if _adjusted(t) < 0)
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def max_drawdown(trades: Sequence[Mapping]) -> float:
    """Peak-to-trough drop on the cumulative adjusted-P&L equity curve."""
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for t in trades:
        equity += _adjusted(t)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _sharpe(returns: Sequence[float]) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    return 0.0 if std == 0 else mean / std


def _sortino(returns: Sequence[float]) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    downside = [r for r in returns if r < 0]
    if not downside:
        return math.inf if mean > 0 else 0.0
    dd = math.sqrt(sum(r ** 2 for r in downside) / len(downside))
    return 0.0 if dd == 0 else mean / dd


def core_metrics(trades: Sequence[Mapping]) -> CoreMetrics:
    """Full core performance block computed from ``adjusted_pnl``."""
    m = CoreMetrics(trade_count=len(trades))
    if not trades:
        return m

    pnls = [_adjusted(t) for t in trades]
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p < 0]

    m.wins = len(win_pnls)
    m.losses = len(loss_pnls)
    m.win_rate = m.wins / m.trade_count
    m.avg_win = (sum(win_pnls) / m.wins) if m.wins else 0.0
    m.avg_loss = (-sum(loss_pnls) / m.losses) if m.losses else 0.0
    m.gross_profit = sum(win_pnls)
    m.gross_loss = -sum(loss_pnls)
    m.expectancy = sum(pnls) / m.trade_count
    m.total_adjusted_pnl = sum(pnls)
    m.profit_factor = profit_factor(trades)
    m.max_drawdown = max_drawdown(trades)
    m.sharpe = _sharpe(pnls)
    m.sortino = _sortino(pnls)
    return m


# ---------------------------------------------------------------------------
# Reality-gap metrics
# ---------------------------------------------------------------------------
def reality_gap_metrics(trades: Sequence[Mapping]) -> RealityGapMetrics:
    g = RealityGapMetrics()
    if not trades:
        return g

    n = len(trades)
    slippage = [_get(t, "modeled_slippage") for t in trades]
    g.cumulative_slippage_cost = sum(slippage)
    g.modeled_slippage_per_trade = g.cumulative_slippage_cost / n
    g.total_fees = sum(_get(t, "fees") for t in trades)
    g.haircut_qty_total = sum(_get(t, "haircut_qty") for t in trades)
    g.total_raw_pnl = sum(_get(t, "raw_pnl") for t in trades)
    g.total_adjusted_pnl = sum(_adjusted(t) for t in trades)
    g.raw_vs_adjusted_gap = g.total_raw_pnl - g.total_adjusted_pnl

    drifts = []
    for t in trades:
        signal = _get(t, "signal_price")
        fill = _get(t, "modeled_fill_price")
        if signal > 0:
            drifts.append(abs(fill - signal) / signal * 1e4)  # basis points
    g.avg_fill_drift_bps = (sum(drifts) / len(drifts)) if drifts else 0.0
    return g


# ---------------------------------------------------------------------------
# Regime split
# ---------------------------------------------------------------------------
def regime_split(trades: Sequence[Mapping]) -> RegimeSplit:
    """Split core metrics by the ``regime`` tag stored on each trade."""
    trending = [t for t in trades if _tag(t) == "trending"]
    choppy = [t for t in trades if _tag(t) == "choppy"]
    return RegimeSplit(core_metrics(trending), core_metrics(choppy))


def _tag(trade: Mapping) -> str:
    try:
        keys = trade.keys()
    except AttributeError:
        keys = ()
    return (trade["regime"] if "regime" in keys else None) or "choppy"


# ---------------------------------------------------------------------------
# Forecasts — computed from adjusted_pnl ONLY (Section 4 rule)
# ---------------------------------------------------------------------------
def forecast(trades: Sequence[Mapping], trades_per_day: float) -> Dict[str, float]:
    """Project weekly / monthly / yearly P&L from per-trade expectancy.

    Uses *adjusted* expectancy only. ``trades_per_day`` sets the cadence; a
    trading year is taken as 252 sessions, a month as 21, a week as 5.
    """
    exp = expectancy(trades)
    daily = exp * trades_per_day
    return {
        "expectancy_per_trade": exp,
        "daily": daily,
        "weekly": daily * 5,
        "monthly": daily * 21,
        "yearly": daily * 252,
    }


@dataclass
class BucketReport:
    core: CoreMetrics
    reality_gap: RealityGapMetrics
    regimes: RegimeSplit


def bucket_report(trades: Sequence[Mapping]) -> BucketReport:
    """One-shot full report block for a bucket (or the whole portfolio)."""
    return BucketReport(
        core=core_metrics(trades),
        reality_gap=reality_gap_metrics(trades),
        regimes=regime_split(trades),
    )
