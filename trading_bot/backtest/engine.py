"""Backtest harness — measure edge on adjusted_pnl before going live.

Walks historical bars forward one at a time, feeds a growing window to the same
deterministic :class:`SignalEngine` the live loop uses, simulates bracket exits
(stop-loss / take-profit / max-hold / opposite-signal), runs every fill through
the Reality Calibration Layer's friction model, and reports on **adjusted_pnl**
— the honest predictor of live performance.

No lookahead: the signal at bar *i* is computed only from bars ``[0..i]`` and the
entry executes at that bar's close (friction then applies slippage + latency on
top). Exit levels (stop/target) are checked against each *subsequent* bar.

This is exactly what the UAT protocol wants a first read on: expectancy, profit
factor, drawdown, and per-regime split over many trades — without waiting four
weeks. A backtest can't replicate live microstructure, but with the friction
layer applied it is far closer than raw paper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..broker.base import Bar, Side
from ..calibration.config import CalibrationConfig
from ..calibration.friction import model_fill, model_round_trip
from ..calibration.regime import ma_slope_regime
from ..db.database import CalibrationDB, TradeRecord
from ..reporting.metrics import BucketReport, bucket_report, forecast
from ..reporting.uat import UATResult, evaluate_bucket
from ..signals.engine import SignalEngine


@dataclass(frozen=True)
class BacktestConfig:
    warmup_bars: int = 50          # bars before the first trade is allowed
    base_notional: float = 5_000.0 # $ per trade, scaled by signal confidence
    min_confidence: float = 0.5
    stop_loss_pct: float = 0.02    # 2% protective stop
    take_profit_pct: float = 0.04  # 4% target (2:1)
    max_holding_bars: int = 60     # force-exit after this many bars
    exit_on_opposite: bool = True  # exit if the engine flips side
    spread_bps: float = 2.0        # quote spread estimate (bars carry no quote)


@dataclass
class BacktestResult:
    trades: List[dict] = field(default_factory=list)   # metrics-compatible rows
    report: Optional[BucketReport] = None
    uat: Optional[UATResult] = None
    forecast: Dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        if self.report is None:
            return "no trades"
        c = self.report.core
        g = self.report.reality_gap
        r = self.report.regimes
        lines = [
            f"trades={c.trade_count}  win_rate={c.win_rate:.1%}  "
            f"expectancy={c.expectancy:.2f}  PF={c.profit_factor:.2f}  "
            f"maxDD={c.max_drawdown:.2f}  Sharpe={c.sharpe:.2f}",
            f"raw_pnl={g.total_raw_pnl:.2f}  adjusted_pnl={g.total_adjusted_pnl:.2f}  "
            f"friction_removed={g.raw_vs_adjusted_gap:.2f}  fees={g.total_fees:.2f}  "
            f"haircut_qty={g.haircut_qty_total:.0f}  fill_drift={g.avg_fill_drift_bps:.1f}bps",
            f"regime expectancy: trending={r.trending.expectancy:.2f} "
            f"({r.trending.trade_count})  choppy={r.choppy.expectancy:.2f} "
            f"({r.choppy.trade_count})",
        ]
        if self.uat is not None:
            lines.append(f"UAT: {self.uat.verdict.value} — {'; '.join(self.uat.reasons)}")
        return "\n".join(lines)


@dataclass
class _Open:
    side: Side
    direction: str
    qty: float
    entry_bar: Bar
    stop: float
    target: float
    regime: str
    bars_held: int = 0


class Backtester:
    """Runs a :class:`SignalEngine` over historical bars with friction applied."""

    def __init__(
        self,
        signal_engine: SignalEngine,
        calib_config: CalibrationConfig,
        bt_config: BacktestConfig = BacktestConfig(),
    ) -> None:
        self.signal_engine = signal_engine
        self.calib = calib_config
        self.cfg = bt_config

    # -- public API --------------------------------------------------------
    def run(
        self,
        bars_by_symbol: Dict[str, Sequence[Bar]],
        db: Optional[CalibrationDB] = None,
        bucket_id: int = 1,
        trades_per_day: float = 5.0,
    ) -> BacktestResult:
        """Backtest every symbol independently and aggregate the trades.

        If ``db`` is given, each round trip is also persisted to the
        ``trades`` table (same schema the live loop writes).
        """
        all_trades: List[dict] = []
        for symbol, bars in bars_by_symbol.items():
            all_trades.extend(self._run_symbol(symbol, list(bars), db, bucket_id))

        result = BacktestResult(trades=all_trades)
        if all_trades:
            result.report = bucket_report(all_trades)
            result.uat = evaluate_bucket(all_trades)
            result.forecast = forecast(all_trades, trades_per_day)
        return result

    # -- per-symbol walk ---------------------------------------------------
    def _run_symbol(self, symbol: str, bars: List[Bar],
                    db: Optional[CalibrationDB], bucket_id: int) -> List[dict]:
        trades: List[dict] = []
        open_lot: Optional[_Open] = None

        for i in range(self.cfg.warmup_bars, len(bars)):
            bar = bars[i]
            window = bars[: i + 1]

            if open_lot is not None:
                exit_price = self._exit_price(open_lot, bar, window)
                if exit_price is not None:
                    trades.append(self._close(symbol, open_lot, bar, exit_price,
                                              db, bucket_id))
                    open_lot = None
                    continue
                open_lot.bars_held += 1

            if open_lot is None:
                signal = self.signal_engine.evaluate(symbol, window)
                if signal is None or signal.confidence < self.cfg.min_confidence:
                    continue
                qty = int(self.cfg.base_notional * signal.confidence / bar.close)
                if qty <= 0:
                    continue
                direction = "long" if signal.side is Side.BUY else "short"
                stop, target = self._bracket(signal.side, bar.close)
                open_lot = _Open(
                    side=signal.side, direction=direction, qty=qty, entry_bar=bar,
                    stop=stop, target=target,
                    regime=ma_slope_regime([b.close for b in window]),
                )

        # Force-close any position still open at the end of the series.
        if open_lot is not None:
            trades.append(self._close(symbol, open_lot, bars[-1], bars[-1].close,
                                      db, bucket_id))
        return trades

    # -- helpers -----------------------------------------------------------
    def _bracket(self, side: Side, price: float):
        if side is Side.BUY:
            return price * (1 - self.cfg.stop_loss_pct), price * (1 + self.cfg.take_profit_pct)
        return price * (1 + self.cfg.stop_loss_pct), price * (1 - self.cfg.take_profit_pct)

    def _exit_price(self, lot: _Open, bar: Bar, window: List[Bar]) -> Optional[float]:
        """Return the exit price if an exit condition triggers on ``bar``.

        Stop/target are checked against the bar's range (conservative: stop is
        checked before target when both are inside the same bar).
        """
        if lot.direction == "long":
            if bar.low <= lot.stop:
                return lot.stop
            if bar.high >= lot.target:
                return lot.target
        else:  # short
            if bar.high >= lot.stop:
                return lot.stop
            if bar.low <= lot.target:
                return lot.target

        if lot.bars_held >= self.cfg.max_holding_bars:
            return bar.close
        if self.cfg.exit_on_opposite:
            sig = self.signal_engine.evaluate(lot.entry_bar.symbol, window)
            if sig is not None and sig.side is not lot.side:
                return bar.close
        return None

    def _close(self, symbol: str, lot: _Open, exit_bar: Bar, exit_price: float,
               db: Optional[CalibrationDB], bucket_id: int) -> dict:
        spread_entry = lot.entry_bar.close * self.cfg.spread_bps / 1e4
        spread_exit = exit_price * self.cfg.spread_bps / 1e4
        entry_fill = model_fill(
            side=lot.side.value, order_qty=lot.qty,
            signal_time_ms=lot.entry_bar.timestamp_ms, signal_price=lot.entry_bar.close,
            spread=spread_entry, bar_volume=lot.entry_bar.volume, config=self.calib,
        )
        exit_side = Side.SELL if lot.direction == "long" else Side.BUY
        exit_fill = model_fill(
            side=exit_side.value, order_qty=lot.qty,
            signal_time_ms=exit_bar.timestamp_ms, signal_price=exit_price,
            spread=spread_exit, bar_volume=exit_bar.volume, config=self.calib,
        )
        friction = model_round_trip(entry_fill, exit_fill, self.calib, direction=lot.direction)

        rec = TradeRecord.from_friction(
            bucket_id, symbol, min(entry_fill.filled_qty, exit_fill.filled_qty),
            friction, direction=lot.direction, regime=lot.regime,
            entry_price=lot.entry_bar.close, exit_price=exit_price,
        )
        if db is not None:
            db.insert_trade(rec)
        # Return a metrics-compatible row (same fields the DB stores).
        return {
            "bucket_id": bucket_id, "symbol": symbol, "direction": lot.direction,
            "qty": rec.qty, "raw_pnl": rec.raw_pnl, "adjusted_pnl": rec.adjusted_pnl,
            "modeled_slippage": rec.modeled_slippage, "fees": rec.fees,
            "latency_ms": rec.latency_ms, "haircut_qty": rec.haircut_qty,
            "regime": rec.regime, "signal_price": rec.signal_price,
            "modeled_fill_price": rec.modeled_fill_price,
        }
