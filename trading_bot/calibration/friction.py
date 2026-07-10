"""Reality Calibration Layer — the friction model (UAT addendum Section 3).

This sits between Alpaca's reported paper fill and what we *record* as the
result. Alpaca paper trading overstates performance: it fills orders larger than
real liquidity, models no slippage, and injects no latency. This module removes
that fake edge so the recorded ``adjusted_pnl`` is an honest predictor of live
performance.

Everything here is a pure function of its inputs plus a
:class:`~trading_bot.calibration.config.CalibrationConfig`, which makes the math
trivially unit-testable and keeps the tunable constants out of the code.

Sign convention
---------------
Slippage always works *against* you:

* a BUY fills *higher* than the reference price (you pay the ask + impact),
* a SELL fills *lower* than the reference price (you receive the bid - impact).
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Literal, Optional, Sequence, Tuple

from .config import CalibrationConfig

Side = Literal["buy", "sell"]


# ---------------------------------------------------------------------------
# 3.1 Slippage
# ---------------------------------------------------------------------------
def half_spread(spread: float, config: CalibrationConfig) -> float:
    """Half the quoted spread, but never less than ``half_spread_min``.

    We conservatively assume we always cross *at least* half the spread.
    """
    if spread < 0:
        raise ValueError("spread must be >= 0")
    return max(spread / 2.0, config.half_spread_min)


def market_impact(order_qty: float, bar_volume: float, config: CalibrationConfig) -> float:
    """Per-share market impact = ``k * (order_qty / minute_bar_volume)``.

    Scales with how large our order is relative to the minute bar's volume:
    trading a big slice of the bar moves the price more.
    """
    if order_qty < 0:
        raise ValueError("order_qty must be >= 0")
    if bar_volume <= 0:
        # No volume in the bar -> treat impact as maximally punitive is unsafe
        # for tests; instead return 0 impact and let the liquidity cap reject the
        # fill entirely (a zero-volume bar caps to zero fillable shares).
        return 0.0
    return config.impact_k * (order_qty / bar_volume)


def slippage_per_share(
    order_qty: float,
    spread: float,
    bar_volume: float,
    config: CalibrationConfig,
) -> float:
    """Total modeled slippage per share = half-spread + market impact."""
    return half_spread(spread, config) + market_impact(order_qty, bar_volume, config)


def apply_slippage(reference_price: float, side: Side, slip: float) -> float:
    """Push ``reference_price`` against the trader by ``slip`` $/share."""
    if side == "buy":
        return reference_price + slip
    if side == "sell":
        return max(reference_price - slip, 0.0)
    raise ValueError(f"unknown side: {side!r}")


# ---------------------------------------------------------------------------
# 3.2 Liquidity cap
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LiquidityResult:
    filled_qty: float
    haircut_qty: float  # rejected remainder, logged for the reality-gap metrics


def apply_liquidity_cap(
    order_qty: float, bar_volume: float, config: CalibrationConfig
) -> LiquidityResult:
    """Haircut any fill that exceeds ``liquidity_cap_pct`` of the bar volume.

    This fixes paper's biggest lie: filling orders larger than real liquidity.
    The rejected remainder is returned as ``haircut_qty`` so the reports can show
    exactly how much fake fill was removed.
    """
    if order_qty < 0:
        raise ValueError("order_qty must be >= 0")
    cap = max(config.liquidity_cap_pct * max(bar_volume, 0.0), 0.0)
    if order_qty <= cap:
        return LiquidityResult(filled_qty=order_qty, haircut_qty=0.0)
    return LiquidityResult(filled_qty=cap, haircut_qty=order_qty - cap)


# ---------------------------------------------------------------------------
# 3.3 Latency injection
# ---------------------------------------------------------------------------
PriceTick = Tuple[int, float]  # (timestamp_ms, price)


def price_at(series: Sequence[PriceTick], target_ms: int) -> Optional[float]:
    """Price at the first tick at-or-after ``target_ms`` in a sorted series.

    Returns ``None`` if no tick exists at/after the target (caller should then
    fall back to the last known price or reject the fill).
    """
    if not series:
        return None
    idx = bisect_left(series, (target_ms, float("-inf")))
    if idx >= len(series):
        return None
    return series[idx][1]


def latency_adjusted_price(
    signal_time_ms: int,
    signal_price: float,
    series: Optional[Sequence[PriceTick]],
    config: CalibrationConfig,
) -> float:
    """Re-price the fill at ``signal_time + latency_ms``.

    If no tick series is available (or none exists at the delayed timestamp), we
    fall back to the signal price — latency then contributes nothing rather than
    guessing, and slippage still applies on top.
    """
    if not series:
        return signal_price
    delayed = price_at(series, signal_time_ms + config.latency_ms)
    return delayed if delayed is not None else signal_price


# ---------------------------------------------------------------------------
# Single-fill friction (combine 3.1 - 3.3)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FillFriction:
    """The outcome of running one order side through the friction model."""

    side: Side
    requested_qty: float
    filled_qty: float
    haircut_qty: float
    signal_price: float
    reference_price: float       # price after latency, before slippage
    modeled_fill_price: float    # what we record as the executed price
    modeled_slippage: float      # per-share slippage applied
    latency_ms: int


def model_fill(
    side: Side,
    order_qty: float,
    signal_time_ms: int,
    signal_price: float,
    spread: float,
    bar_volume: float,
    config: CalibrationConfig,
    price_series: Optional[Sequence[PriceTick]] = None,
) -> FillFriction:
    """Run a single order side through liquidity cap, latency, and slippage."""
    liq = apply_liquidity_cap(order_qty, bar_volume, config)
    reference = latency_adjusted_price(signal_time_ms, signal_price, price_series, config)
    slip = slippage_per_share(liq.filled_qty, spread, bar_volume, config)
    fill_price = apply_slippage(reference, side, slip)
    return FillFriction(
        side=side,
        requested_qty=order_qty,
        filled_qty=liq.filled_qty,
        haircut_qty=liq.haircut_qty,
        signal_price=signal_price,
        reference_price=reference,
        modeled_fill_price=fill_price,
        modeled_slippage=slip,
        latency_ms=config.latency_ms,
    )


# ---------------------------------------------------------------------------
# Round-trip P&L (raw vs adjusted) — what lands in the trades table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TradeFriction:
    """A closed round trip with both P&L tracks and all reality-gap fields.

    ``raw_pnl`` is exactly what Alpaca paper would report (no friction).
    ``adjusted_pnl`` is after friction and is the only track forecasts use.
    """

    raw_pnl: float
    adjusted_pnl: float
    modeled_slippage: float       # total $/share slippage across both sides
    fees: float
    latency_ms: int
    haircut_qty: float            # total shares rejected by the liquidity cap
    signal_price: float           # entry signal price (fill-quality reference)
    modeled_fill_price: float     # entry modeled fill price

    @property
    def pnl_gap(self) -> float:
        """How much fake edge friction removed (raw - adjusted)."""
        return self.raw_pnl - self.adjusted_pnl


def model_round_trip(
    entry: FillFriction,
    exit: FillFriction,
    config: CalibrationConfig,
    direction: Literal["long", "short"] = "long",
) -> TradeFriction:
    """Combine entry + exit fills into raw and adjusted P&L for one trade.

    ``raw_pnl`` uses the *signal* prices (Alpaca's frictionless view). The number
    of shares is the smaller of the two legs' filled quantities — you cannot
    close more than you opened.
    """
    qty = min(entry.filled_qty, exit.filled_qty)
    if direction == "long":
        raw_pnl = (exit.signal_price - entry.signal_price) * qty
        adj_gross = (exit.modeled_fill_price - entry.modeled_fill_price) * qty
        sell_proceeds = exit.modeled_fill_price * qty
    elif direction == "short":
        raw_pnl = (entry.signal_price - exit.signal_price) * qty
        adj_gross = (entry.modeled_fill_price - exit.modeled_fill_price) * qty
        # For a short the opening leg is the sell that generates proceeds.
        sell_proceeds = entry.modeled_fill_price * qty
    else:
        raise ValueError(f"unknown direction: {direction!r}")

    fees = config.fees.sell_fees(sell_proceeds, qty)
    adjusted_pnl = adj_gross - fees
    total_slippage = entry.modeled_slippage + exit.modeled_slippage
    total_haircut = entry.haircut_qty + exit.haircut_qty

    return TradeFriction(
        raw_pnl=raw_pnl,
        adjusted_pnl=adjusted_pnl,
        modeled_slippage=total_slippage,
        fees=fees,
        latency_ms=config.latency_ms,
        haircut_qty=total_haircut,
        signal_price=entry.signal_price,
        modeled_fill_price=entry.modeled_fill_price,
    )
