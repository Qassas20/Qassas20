"""Risk gates and position sizing (Integration Spec §4).

These are the guards the loop enforces every tick, on top of the calibration
layer. MarketContext modulates sizing and a take/skip filter — it never places
trades. Per-bucket capital and portfolio heat come from the BucketLedger.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..broker.accounting import BucketLedger
from ..intelligence.market_intelligence import MarketContext, Sentiment, VolLevel
from ..signals.engine import Signal
from ..broker.base import Side


@dataclass(frozen=True)
class RiskConfig:
    base_position_notional: float = 1_000.0   # $ per trade before modulation
    min_confidence: float = 0.5               # skip signals weaker than this
    high_vol_confidence: float = 0.7          # stricter gate in high volatility
    stop_loss_pct: float = 0.02               # 2% stop -> defines per-trade risk
    daily_loss_limit: float = 500.0           # per bucket; hit it -> pause bucket


@dataclass
class BucketRuntime:
    """Mutable per-bucket state the loop threads through the guards."""

    bucket_id: int
    realized_pnl_today: float = 0.0
    paused: bool = False


def passes_risk(runtime: BucketRuntime, ctx: MarketContext, signal: Signal,
                config: RiskConfig) -> bool:
    """Confidence + regime + daily-loss gate for a single signal."""
    if runtime.paused:
        return False
    if runtime.realized_pnl_today <= -abs(config.daily_loss_limit):
        return False

    # Confidence floor, raised in high volatility.
    floor = config.min_confidence
    if ctx.volatility is VolLevel.HIGH:
        floor = config.high_vol_confidence
    if signal.confidence < floor:
        return False

    # Sentiment/side alignment: skip low-confidence trades that fight sentiment.
    sym_sentiment = ctx.per_symbol.get(signal.symbol, ctx.sentiment)
    if _fights_sentiment(signal.side, sym_sentiment) and signal.confidence < 0.75:
        return False
    return True


def _fights_sentiment(side: Side, sentiment: Sentiment) -> bool:
    if side is Side.BUY and sentiment is Sentiment.BEARISH:
        return True
    if side is Side.SELL and sentiment is Sentiment.BULLISH:
        return True
    return False


def size_position(ctx: MarketContext, signal: Signal, price: float,
                  config: RiskConfig) -> float:
    """Quantity to trade, modulated by confidence, sentiment, and volatility.

    Bearish + high-vol → halve; strong aligned sentiment → up to +25%. Returns a
    whole-share quantity (floor); may be 0 if the modulated notional < 1 share.
    """
    if price <= 0:
        return 0.0
    notional = config.base_position_notional * signal.confidence

    if ctx.volatility is VolLevel.HIGH:
        notional *= 0.5
    elif ctx.volatility is VolLevel.LOW:
        notional *= 1.1

    sym_sentiment = ctx.per_symbol.get(signal.symbol, ctx.sentiment)
    if not _fights_sentiment(signal.side, sym_sentiment) and sym_sentiment is not Sentiment.NEUTRAL:
        notional *= 1.25   # aligned conviction
    elif _fights_sentiment(signal.side, sym_sentiment):
        notional *= 0.5    # trimmed when leaning against sentiment

    return float(int(notional / price))  # whole shares


def per_trade_risk(qty: float, price: float, config: RiskConfig) -> float:
    """Dollar risk of a position given the stop distance (for portfolio heat)."""
    return qty * price * config.stop_loss_pct


def within_bucket_capital(ledger: BucketLedger, bucket_id: int, qty: float,
                          price: float) -> bool:
    """Enforce the bucket's capital ceiling (Alpaca won't — single account)."""
    return ledger.can_deploy(bucket_id, qty, price)


def stop_prices(side: Side, entry_price: float, config: RiskConfig):
    """(stop_loss, take_profit) for a bracket order at 2%/4% by default."""
    if side is Side.BUY:
        return entry_price * (1 - config.stop_loss_pct), entry_price * (1 + 2 * config.stop_loss_pct)
    return entry_price * (1 + config.stop_loss_pct), entry_price * (1 - 2 * config.stop_loss_pct)
