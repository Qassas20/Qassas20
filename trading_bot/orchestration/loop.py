"""The orchestration loop (Integration Spec §4) — the glue.

Ties the three integrations together, per bucket, on each bar:

    ctx = intelligence.assess(watchlist)          # cached, cheap
    for symbol in bucket.watchlist:
        bars   = broker.get_bars(...)
        signal = signal_engine.evaluate(...)      # deterministic
        gate by risk + confidence + sentiment
        size, enforce bucket capital + portfolio heat
        submit order (bracket with stops)
        record fill -> calibration raw_pnl / adjusted_pnl
    enforce per-position stops + DAILY LOSS CUTOFF

Every fill is fed into the existing Reality Calibration Layer via
:class:`TradeRecorder`, so raw and adjusted P&L are tracked exactly as in Phase
5/7. Bucket isolation is preserved end to end: capital ceilings, portfolio heat,
and all close operations are scoped by ``bucket_id``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..broker.accounting import BucketLedger
from ..broker.base import Bar, BrokerClient, OrderType, Side
from ..calibration.config import CalibrationConfig
from ..calibration.friction import model_fill, model_round_trip, FillFriction
from ..calibration.regime import ma_slope_regime
from ..db.database import CalibrationDB, TradeRecord
from ..intelligence.market_intelligence import MarketContext, MarketIntelligence
from ..signals.engine import Signal, SignalEngine
from . import risk as R


@dataclass
class Bucket:
    id: int
    watchlist: List[str]
    capital_ceiling: float


@dataclass
class _OpenLot:
    """An open entry awaiting its exit, holding the entry-side friction."""

    entry: FillFriction
    qty: float
    price: float
    risk: float
    regime: str
    direction: str


class TradeRecorder:
    """Feeds fills into the calibration layer, tracking raw + adjusted P&L.

    On entry it models the entry fill and holds it; on exit it models the exit
    fill, computes the round trip, and persists a :class:`TradeRecord`. Spread is
    estimated in basis points of price (paper bars carry no quote), and the
    regime tag is computed from the bars via the calibration regime module.
    """

    def __init__(
        self,
        db: CalibrationDB,
        config: CalibrationConfig,
        spread_bps: float = 2.0,
        clock: Optional[Callable[[], int]] = None,
    ) -> None:
        self.db = db
        self.config = config
        self.spread_bps = spread_bps
        self._clock = clock or _default_clock
        self._open: Dict[tuple, _OpenLot] = {}   # (bucket_id, symbol) -> lot

    def _spread(self, price: float) -> float:
        return price * self.spread_bps / 1e4

    def record_entry(self, bucket_id: int, symbol: str, side: Side, qty: float,
                     bar: Bar, regime: str, direction: str = "long") -> None:
        entry = model_fill(
            side=side.value, order_qty=qty, signal_time_ms=bar.timestamp_ms,
            signal_price=bar.close, spread=self._spread(bar.close),
            bar_volume=bar.volume, config=self.config,
        )
        self._open[(bucket_id, symbol)] = _OpenLot(
            entry=entry, qty=qty, price=bar.close, risk=0.0,
            regime=regime, direction=direction,
        )

    def record_exit(self, bucket_id: int, symbol: str, bar: Bar) -> Optional[int]:
        """Model the exit, persist the round trip, return the trade row id."""
        lot = self._open.pop((bucket_id, symbol), None)
        if lot is None:
            return None
        exit_side = Side.SELL if lot.direction == "long" else Side.BUY
        exit_fill = model_fill(
            side=exit_side.value, order_qty=lot.qty, signal_time_ms=bar.timestamp_ms,
            signal_price=bar.close, spread=self._spread(bar.close),
            bar_volume=bar.volume, config=self.config,
        )
        friction = model_round_trip(lot.entry, exit_fill, self.config, direction=lot.direction)
        rec = TradeRecord.from_friction(
            bucket_id, symbol, min(lot.entry.filled_qty, exit_fill.filled_qty),
            friction, direction=lot.direction, regime=lot.regime,
        )
        return self.db.insert_trade(rec)

    def has_open(self, bucket_id: int, symbol: str) -> bool:
        return (bucket_id, symbol) in self._open


@dataclass
class LoopResult:
    submitted: int = 0
    skipped: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)

    def _skip(self, reason: str) -> None:
        self.skipped += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class TradingLoop:
    def __init__(
        self,
        broker: BrokerClient,
        signal_engine: SignalEngine,
        intelligence: MarketIntelligence,
        ledger: BucketLedger,
        recorder: TradeRecorder,
        risk_config: R.RiskConfig = R.RiskConfig(),
        bars_limit: int = 200,
    ) -> None:
        self.broker = broker
        self.signal_engine = signal_engine
        self.intelligence = intelligence
        self.ledger = ledger
        self.recorder = recorder
        self.risk_config = risk_config
        self.bars_limit = bars_limit
        self._runtimes: Dict[int, R.BucketRuntime] = {}

    def runtime(self, bucket_id: int) -> R.BucketRuntime:
        if bucket_id not in self._runtimes:
            self._runtimes[bucket_id] = R.BucketRuntime(bucket_id=bucket_id)
        return self._runtimes[bucket_id]

    def run_bucket_once(self, bucket: Bucket) -> LoopResult:
        """One tick for one bucket. Returns a summary of what happened."""
        result = LoopResult()
        runtime = self.runtime(bucket.id)

        # Daily-loss cutoff: pause this bucket only, touching no other bucket.
        if runtime.realized_pnl_today <= -abs(self.risk_config.daily_loss_limit):
            runtime.paused = True
        if runtime.paused:
            result._skip("bucket_paused")
            return result

        ctx: MarketContext = self.intelligence.assess(bucket.watchlist)

        for symbol in bucket.watchlist:
            bars = self.broker.get_bars(symbol, "1Min", self.bars_limit)
            if not bars:
                result._skip("no_bars")
                continue
            signal = self.signal_engine.evaluate(symbol, bars)
            if signal is None:
                result._skip("no_signal")
                continue
            if not R.passes_risk(runtime, ctx, signal, self.risk_config):
                result._skip("risk_gate")
                continue

            price = bars[-1].close
            qty = R.size_position(ctx, signal, price, self.risk_config)
            if qty <= 0:
                result._skip("zero_qty")
                continue

            trade_risk = R.per_trade_risk(qty, price, self.risk_config)
            if not R.within_bucket_capital(self.ledger, bucket.id, qty, price):
                result._skip("bucket_capital")
                continue
            if not self.ledger.within_portfolio_heat(trade_risk):
                result._skip("portfolio_heat")
                continue

            sl, tp = R.stop_prices(signal.side, price, self.risk_config)
            self.broker.submit_order(
                bucket.id, symbol, signal.side, qty, OrderType.MARKET,
                stop_loss=sl, take_profit=tp,
            )
            self.ledger.on_open(bucket.id, qty, price, trade_risk)
            regime = ma_slope_regime([b.close for b in bars])
            direction = "long" if signal.side is Side.BUY else "short"
            self.recorder.record_entry(bucket.id, symbol, signal.side, qty,
                                       bars[-1], regime, direction)
            result.submitted += 1

        return result

    def enforce_stops(self, bucket: Bucket) -> None:
        """Per-bucket daily-loss cutoff check (per-position stops ride on the
        bracket orders submitted at entry). Pauses only this bucket."""
        runtime = self.runtime(bucket.id)
        if runtime.realized_pnl_today <= -abs(self.risk_config.daily_loss_limit):
            runtime.paused = True

    def record_realized(self, bucket_id: int, pnl: float) -> None:
        """Update a bucket's realized daily P&L (drives the daily-loss cutoff)."""
        self.runtime(bucket_id).realized_pnl_today += pnl


def _default_clock() -> int:
    import time
    return int(time.time() * 1000)
