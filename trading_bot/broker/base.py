"""Broker port + shared data models (Integration Spec Section 1).

Defines the ``BrokerClient`` interface the engine codes against (adapter
pattern), plus the small value types that flow through it. The Alpaca
implementation lives in :mod:`trading_bot.broker.alpaca`; tests use a fake
implementing this same ABC.

Mirrors the Kotlin ``BrokerClient`` interface from the spec 1:1 so a Kotlin
Multiplatform port is mechanical.

Key reality the engine must own (Alpaca is a *single* account):
* buying power is shared across the whole account — per-bucket capital limits
  are enforced in our own accounting layer (:mod:`trading_bot.broker.accounting`),
  never by the broker;
* fills are attributed to buckets by parsing the ``client_order_id`` prefix.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar (e.g. 1-minute) from the market-data feed."""

    symbol: str
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Account:
    cash: float
    buying_power: float          # shared across ALL buckets — the trap
    equity: float
    currency: str = "USD"


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float
    bucket_id: Optional[int] = None   # attributed via client_order_id prefix


@dataclass(frozen=True)
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: float
    order_type: OrderType
    status: str
    filled_qty: float = 0.0
    filled_avg_price: Optional[float] = None
    bucket_id: Optional[int] = None


# ---------------------------------------------------------------------------
# client_order_id convention: "b{bucket_id}_{symbol}_{epoch_ms}"
# This is how a single Alpaca account is partitioned back into buckets.
# ---------------------------------------------------------------------------
def make_client_order_id(bucket_id: int, symbol: str, epoch_ms: int) -> str:
    return f"b{bucket_id}_{symbol}_{epoch_ms}"


def parse_bucket_id(client_order_id: Optional[str]) -> Optional[int]:
    """Recover the bucket id from a ``b{id}_...`` client_order_id, or ``None``."""
    if not client_order_id or not client_order_id.startswith("b"):
        return None
    head = client_order_id.split("_", 1)[0]  # "b2"
    try:
        return int(head[1:])
    except (ValueError, IndexError):
        return None


class BrokerClient(ABC):
    """The port the engine codes against (spec Section 1).

    Every order-placing method is bucket-scoped and every position query is
    filterable by bucket, so bucket isolation is preserved end to end.
    """

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: str, limit: int) -> List[Bar]:
        """Historical bars for indicator warmup / backtest (REST)."""

    @abstractmethod
    def stream_bars(self, symbols: List[str], on_bar: Callable[[Bar], None]) -> None:
        """Subscribe to live bars (WebSocket) and invoke ``on_bar`` per bar."""

    @abstractmethod
    def get_account(self) -> Account:
        ...

    @abstractmethod
    def get_positions(self, bucket_id: int) -> List[Position]:
        """Open positions attributed to ``bucket_id`` (via client_order_id)."""

    @abstractmethod
    def submit_order(
        self,
        bucket_id: int,
        symbol: str,
        side: Side,
        qty: float,
        order_type: OrderType = OrderType.MARKET,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Order:
        ...

    @abstractmethod
    def close_position(self, bucket_id: int, symbol: str) -> None:
        """Close a single bucket+symbol position. Never account-wide."""
