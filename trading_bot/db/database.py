"""SQLite persistence for the Reality Calibration Layer.

Thin, dependency-free wrapper over :mod:`sqlite3`. Handles:

* schema creation / idempotent migration,
* persisting closed trades with both P&L tracks,
* reading and writing the tunable :class:`CalibrationConfig`,
* logging shadow-live paper-vs-live gaps (Section 6).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Iterable, List, Optional

from ..calibration.config import CalibrationConfig, FeeSchedule
from ..calibration.friction import TradeFriction

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


@dataclass
class TradeRecord:
    """A closed round trip as stored in the ``trades`` table."""

    bucket_id: int
    symbol: str
    qty: float
    direction: str = "long"
    entry_time: Optional[str] = None
    exit_time: Optional[str] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    raw_pnl: Optional[float] = None
    adjusted_pnl: Optional[float] = None
    modeled_slippage: Optional[float] = None
    fees: Optional[float] = None
    latency_ms: Optional[int] = None
    haircut_qty: Optional[float] = None
    regime: Optional[str] = None
    signal_price: Optional[float] = None
    modeled_fill_price: Optional[float] = None
    id: Optional[int] = None

    @classmethod
    def from_friction(
        cls,
        bucket_id: int,
        symbol: str,
        qty: float,
        friction: TradeFriction,
        *,
        direction: str = "long",
        regime: Optional[str] = None,
        entry_time: Optional[str] = None,
        exit_time: Optional[str] = None,
        entry_price: Optional[float] = None,
        exit_price: Optional[float] = None,
    ) -> "TradeRecord":
        """Build a record from a :class:`TradeFriction` result."""
        return cls(
            bucket_id=bucket_id,
            symbol=symbol,
            qty=qty,
            direction=direction,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            raw_pnl=friction.raw_pnl,
            adjusted_pnl=friction.adjusted_pnl,
            modeled_slippage=friction.modeled_slippage,
            fees=friction.fees,
            latency_ms=friction.latency_ms,
            haircut_qty=friction.haircut_qty,
            regime=regime,
            signal_price=friction.signal_price,
            modeled_fill_price=friction.modeled_fill_price,
        )


class CalibrationDB:
    """Owns a SQLite connection and the calibration-layer read/write helpers."""

    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
            self.conn.executescript(fh.read())
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CalibrationDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- buckets -----------------------------------------------------------
    def add_bucket(
        self,
        name: str,
        risk_tier: Optional[str] = None,
        max_drawdown: Optional[float] = None,
        daily_stop: Optional[float] = None,
        created_at: Optional[str] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO buckets (name, risk_tier, max_drawdown, daily_stop, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, risk_tier, max_drawdown, daily_stop, created_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # -- trades ------------------------------------------------------------
    def insert_trade(self, rec: TradeRecord) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO trades (
                bucket_id, symbol, direction, qty, entry_time, exit_time,
                entry_price, exit_price, raw_pnl, adjusted_pnl, modeled_slippage,
                fees, latency_ms, haircut_qty, regime, signal_price,
                modeled_fill_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.bucket_id, rec.symbol, rec.direction, rec.qty, rec.entry_time,
                rec.exit_time, rec.entry_price, rec.exit_price, rec.raw_pnl,
                rec.adjusted_pnl, rec.modeled_slippage, rec.fees, rec.latency_ms,
                rec.haircut_qty, rec.regime, rec.signal_price, rec.modeled_fill_price,
            ),
        )
        self.conn.commit()
        rec.id = int(cur.lastrowid)
        return rec.id

    def trades_for_bucket(self, bucket_id: int) -> List[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM trades WHERE bucket_id = ? ORDER BY id", (bucket_id,)
            )
        )

    def all_trades(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM trades ORDER BY id"))

    # -- calibration config ------------------------------------------------
    def save_config(self, config: CalibrationConfig, updated_at: Optional[str] = None) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO calibration_config (
                impact_k, liquidity_cap_pct, latency_ms, half_spread_min,
                sec_fee_per_dollar, finra_taf_per_share, finra_taf_max, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                config.impact_k, config.liquidity_cap_pct, config.latency_ms,
                config.half_spread_min, config.fees.sec_fee_per_dollar,
                config.fees.finra_taf_per_share, config.fees.finra_taf_max, updated_at,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest_config(self) -> Optional[CalibrationConfig]:
        """Most recently written calibration config, or ``None`` if unset."""
        row = self.conn.execute(
            "SELECT * FROM calibration_config ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return CalibrationConfig(
            impact_k=row["impact_k"],
            liquidity_cap_pct=row["liquidity_cap_pct"],
            latency_ms=row["latency_ms"],
            half_spread_min=row["half_spread_min"],
            fees=FeeSchedule(
                sec_fee_per_dollar=row["sec_fee_per_dollar"],
                finra_taf_per_share=row["finra_taf_per_share"],
                finra_taf_max=row["finra_taf_max"],
            ),
        )

    # -- shadow-live gap tracking -----------------------------------------
    def log_live_vs_paper(
        self,
        bucket_id: int,
        symbol: str,
        timestamp: str,
        paper_adjusted_pnl: float,
        live_actual_pnl: float,
    ) -> int:
        gap = live_actual_pnl - paper_adjusted_pnl
        cur = self.conn.execute(
            """
            INSERT INTO live_vs_paper (
                bucket_id, symbol, timestamp, paper_adjusted_pnl,
                live_actual_pnl, gap
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (bucket_id, symbol, timestamp, paper_adjusted_pnl, live_actual_pnl, gap),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def live_vs_paper_rows(self, bucket_id: Optional[int] = None) -> List[sqlite3.Row]:
        if bucket_id is None:
            return list(self.conn.execute("SELECT * FROM live_vs_paper ORDER BY id"))
        return list(
            self.conn.execute(
                "SELECT * FROM live_vs_paper WHERE bucket_id = ? ORDER BY id",
                (bucket_id,),
            )
        )
