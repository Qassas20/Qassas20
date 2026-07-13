"""Alpaca broker adapter — REST + WebSocket (Integration Spec Section 1).

Implements :class:`BrokerClient` against Alpaca's Trading and Market-Data APIs.
Paper trading uses the paper base URL and the IEX data feed (all a paper account
gets). Every order is tagged with a bucket-prefixed ``client_order_id`` so fills
reconcile back to the right bucket; positions are filtered to a bucket the same
way.

The HTTP transport and the WebSocket stream source are **injected**, so the whole
adapter is unit-testable with a fake and no network / no API keys. The default
transport uses stdlib ``urllib`` (no third-party dependency); the default stream
source raises unless a real WebSocket source is supplied.

Credentials come from an injected keystore and are sent as Alpaca's
``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` headers — never logged, never
stored on the instance, never in the DB.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional

from .base import (
    Account,
    Bar,
    BrokerClient,
    Order,
    OrderType,
    Position,
    Side,
    make_client_order_id,
    parse_bucket_id,
)

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

# transport(method, url, headers, body_dict|None) -> parsed JSON (dict|list)
Transport = Callable[[str, str, Dict[str, str], Optional[dict]], object]
# stream_source(url, headers, symbols, on_message) -> None (blocks)
StreamSource = Callable[[str, Dict[str, str], List[str], Callable[[dict], None]], None]
Clock = Callable[[], int]  # -> epoch milliseconds


class AlpacaError(RuntimeError):
    pass


def _urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[dict]) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:  # 4xx/5xx
        detail = exc.read().decode(errors="replace")
        raise AlpacaError(f"{method} {url} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AlpacaError(f"{method} {url} failed: {exc.reason}") from exc
    return json.loads(raw) if raw else {}


def _default_clock() -> int:
    return int(time.time() * 1000)


class AlpacaBrokerClient(BrokerClient):
    def __init__(
        self,
        keystore: Callable[[], Dict[str, str]],
        paper: bool = True,
        transport: Optional[Transport] = None,
        stream_source: Optional[StreamSource] = None,
        clock: Optional[Clock] = None,
        feed: str = "iex",  # paper accounts get IEX only
    ) -> None:
        self._keystore = keystore
        self.trading_url = PAPER_TRADING_URL if paper else LIVE_TRADING_URL
        self.data_url = DATA_URL
        self.feed = feed
        self._transport = transport or _urllib_transport
        self._stream_source = stream_source
        self._clock = clock or _default_clock

    # -- auth --------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        creds = self._keystore()  # fetched on demand, never stored/logged
        return {
            "APCA-API-KEY-ID": creds["api_key_id"],
            "APCA-API-SECRET-KEY": creds["api_secret_key"],
            "Content-Type": "application/json",
        }

    def _trading(self, method: str, path: str, body: Optional[dict] = None) -> object:
        return self._transport(method, f"{self.trading_url}{path}", self._headers(), body)

    def _data(self, method: str, path: str, body: Optional[dict] = None) -> object:
        return self._transport(method, f"{self.data_url}{path}", self._headers(), body)

    # -- market data -------------------------------------------------------
    def get_bars(
        self,
        symbol: str,
        timeframe: str = "1Min",
        limit: int = 200,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[Bar]:
        """Historical bars, paginating via ``next_page_token`` until ``limit``.

        Alpaca caps each page at 10,000 bars, so a backtest wanting more follows
        the page token. ``start``/``end`` (RFC3339) bound a historical range;
        omit them to get the most recent ``limit`` bars.
        """
        collected: List[Bar] = []
        page_token: Optional[str] = None
        remaining = limit
        while remaining > 0:
            page_limit = min(remaining, 10_000)
            params = [f"timeframe={timeframe}", f"limit={page_limit}", f"feed={self.feed}"]
            if start:
                params.append(f"start={start}")
            if end:
                params.append(f"end={end}")
            if page_token:
                params.append(f"page_token={page_token}")
            raw = self._data("GET", f"/v2/stocks/{symbol}/bars?" + "&".join(params))
            if not isinstance(raw, dict):
                break
            for b in raw.get("bars") or []:
                collected.append(Bar(
                    symbol=symbol, timestamp_ms=_ts_to_ms(b.get("t")),
                    open=b["o"], high=b["h"], low=b["l"], close=b["c"], volume=b["v"],
                ))
            remaining = limit - len(collected)
            page_token = raw.get("next_page_token")
            if not page_token:
                break
        return collected[:limit]

    def stream_bars(self, symbols: List[str], on_bar: Callable[[Bar], None]) -> None:
        if self._stream_source is None:
            raise AlpacaError(
                "no stream_source configured; inject a WebSocket source to stream bars"
            )
        url = f"wss://stream.data.alpaca.markets/v2/{self.feed}"

        def _on_message(msg: dict) -> None:
            if msg.get("T") == "b":  # bar message
                on_bar(Bar(
                    symbol=msg["S"], timestamp_ms=_ts_to_ms(msg.get("t")),
                    open=msg["o"], high=msg["h"], low=msg["l"], close=msg["c"], volume=msg["v"],
                ))

        self._stream_source(url, self._headers(), symbols, _on_message)

    # -- account / positions ----------------------------------------------
    def get_account(self) -> Account:
        raw = self._trading("GET", "/v2/account")
        return Account(
            cash=float(raw["cash"]),
            buying_power=float(raw["buying_power"]),
            equity=float(raw["equity"]),
            currency=raw.get("currency", "USD"),
        )

    def get_positions(self, bucket_id: int) -> List[Position]:
        """All positions, but only those this bucket opened.

        Alpaca positions don't carry a bucket tag, so we reconcile via each
        position's open orders' client_order_id prefix.
        """
        owned = self._symbols_owned_by_bucket(bucket_id)
        raw = self._trading("GET", "/v2/positions")
        rows = raw if isinstance(raw, list) else []
        out: List[Position] = []
        for p in rows:
            if p["symbol"] in owned:
                out.append(Position(
                    symbol=p["symbol"], qty=float(p["qty"]),
                    avg_entry_price=float(p["avg_entry_price"]),
                    market_value=float(p["market_value"]),
                    unrealized_pl=float(p["unrealized_pl"]),
                    bucket_id=bucket_id,
                ))
        return out

    def _symbols_owned_by_bucket(self, bucket_id: int) -> set:
        raw = self._trading("GET", "/v2/orders?status=all&limit=500")
        rows = raw if isinstance(raw, list) else []
        return {
            o["symbol"] for o in rows
            if parse_bucket_id(o.get("client_order_id")) == bucket_id
        }

    # -- orders ------------------------------------------------------------
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
        coid = make_client_order_id(bucket_id, symbol, self._clock())
        body: dict = {
            "symbol": symbol,
            "qty": qty,
            "side": side.value,
            "type": order_type.value,
            "time_in_force": "day",
            "client_order_id": coid,
        }
        # Bracket order when stops/targets are supplied.
        if stop_loss is not None or take_profit is not None:
            body["order_class"] = "bracket"
            if take_profit is not None:
                body["take_profit"] = {"limit_price": take_profit}
            if stop_loss is not None:
                body["stop_loss"] = {"stop_price": stop_loss}
        raw = self._trading("POST", "/v2/orders", body)
        return _order_from_raw(raw, bucket_id)

    def close_position(self, bucket_id: int, symbol: str) -> None:
        """Close one bucket+symbol position — scoped, never account-wide.

        Guards against closing a symbol this bucket doesn't own (which would
        mean touching another bucket's position).
        """
        if symbol not in self._symbols_owned_by_bucket(bucket_id):
            raise AlpacaError(
                f"bucket {bucket_id} does not own {symbol}; refusing cross-bucket close"
            )
        self._trading("DELETE", f"/v2/positions/{symbol}")


def _ts_to_ms(ts) -> int:
    """Alpaca timestamps are RFC3339 strings; fall back to 0 if absent."""
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        return int(ts)
    # "2026-07-10T14:30:00Z" -> epoch ms, without pulling in heavy deps
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


def _order_from_raw(raw: dict, bucket_id: int) -> Order:
    return Order(
        id=str(raw.get("id", "")),
        client_order_id=raw.get("client_order_id", ""),
        symbol=raw.get("symbol", ""),
        side=Side(raw.get("side", "buy")),
        qty=float(raw.get("qty", 0) or 0),
        order_type=OrderType(raw.get("type", "market")),
        status=raw.get("status", "new"),
        filled_qty=float(raw.get("filled_qty", 0) or 0),
        filled_avg_price=(float(raw["filled_avg_price"]) if raw.get("filled_avg_price") else None),
        bucket_id=bucket_id,
    )
