"""Unit tests for the Alpaca broker adapter (Integration Spec §1), mocked."""

import unittest

from trading_bot.broker.base import (
    OrderType,
    Side,
    make_client_order_id,
    parse_bucket_id,
)
from trading_bot.broker.alpaca import AlpacaBrokerClient, PAPER_TRADING_URL, AlpacaError


KEYSTORE = lambda: {"api_key_id": "k", "api_secret_key": "s"}


class MockTransport:
    """Records requests; returns canned responses keyed by (method, path-substr)."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        for (m, sub), resp in self.responses.items():
            if m == method and sub in url:
                return resp(body) if callable(resp) else resp
        return {}


class TestClientOrderId(unittest.TestCase):
    def test_roundtrip(self):
        coid = make_client_order_id(3, "AAPL", 1699999999000)
        self.assertTrue(coid.startswith("b3_AAPL_"))
        self.assertEqual(parse_bucket_id(coid), 3)

    def test_parse_bad(self):
        self.assertIsNone(parse_bucket_id(None))
        self.assertIsNone(parse_bucket_id("garbage"))
        self.assertIsNone(parse_bucket_id("bx_AAPL_1"))


class TestSubmitOrder(unittest.TestCase):
    def test_paper_url_and_prefix(self):
        transport = MockTransport({
            ("POST", "/v2/orders"): lambda body: {
                "id": "o1", "client_order_id": body["client_order_id"],
                "symbol": body["symbol"], "side": body["side"], "qty": body["qty"],
                "type": body["type"], "status": "new",
            },
        })
        broker = AlpacaBrokerClient(KEYSTORE, paper=True, transport=transport,
                                    clock=lambda: 1699999999000)
        order = broker.submit_order(2, "AAPL", Side.BUY, 10)
        # Hit the paper URL
        self.assertIn(PAPER_TRADING_URL, transport.calls[-1][1])
        # client_order_id carries the bucket prefix
        self.assertTrue(order.client_order_id.startswith("b2_AAPL_"))
        self.assertEqual(parse_bucket_id(order.client_order_id), 2)

    def test_bracket_when_stops_given(self):
        captured = {}
        def handler(body):
            captured.update(body)
            return {"id": "o1", "client_order_id": body["client_order_id"],
                    "symbol": "AAPL", "side": "buy", "qty": 10, "type": "market",
                    "status": "new"}
        transport = MockTransport({("POST", "/v2/orders"): handler})
        broker = AlpacaBrokerClient(KEYSTORE, transport=transport, clock=lambda: 1)
        broker.submit_order(1, "AAPL", Side.BUY, 10, OrderType.MARKET,
                            stop_loss=95.0, take_profit=110.0)
        self.assertEqual(captured["order_class"], "bracket")
        self.assertEqual(captured["stop_loss"]["stop_price"], 95.0)
        self.assertEqual(captured["take_profit"]["limit_price"], 110.0)

    def test_credentials_in_headers_not_body(self):
        transport = MockTransport({("POST", "/v2/orders"): {"id": "o", "symbol": "AAPL",
                                    "side": "buy", "qty": 1, "type": "market", "status": "new",
                                    "client_order_id": "b1_AAPL_1"}})
        broker = AlpacaBrokerClient(KEYSTORE, transport=transport, clock=lambda: 1)
        broker.submit_order(1, "AAPL", Side.BUY, 1)
        method, url, headers, body = transport.calls[-1]
        self.assertEqual(headers["APCA-API-KEY-ID"], "k")
        self.assertNotIn("api_secret_key", body)


class TestBucketScoping(unittest.TestCase):
    def _broker_with_orders(self, orders, positions=None, extra=None):
        responses = {
            ("GET", "/v2/orders"): orders,
            ("GET", "/v2/positions"): positions or [],
        }
        responses.update(extra or {})
        return AlpacaBrokerClient(KEYSTORE, transport=MockTransport(responses),
                                  clock=lambda: 1), responses

    def test_positions_filtered_to_bucket(self):
        orders = [
            {"symbol": "AAPL", "client_order_id": "b1_AAPL_1"},
            {"symbol": "TSLA", "client_order_id": "b2_TSLA_1"},  # other bucket
        ]
        positions = [
            {"symbol": "AAPL", "qty": "10", "avg_entry_price": "100",
             "market_value": "1000", "unrealized_pl": "5"},
            {"symbol": "TSLA", "qty": "5", "avg_entry_price": "200",
             "market_value": "1000", "unrealized_pl": "-5"},
        ]
        broker, _ = self._broker_with_orders(orders, positions)
        got = broker.get_positions(1)
        self.assertEqual([p.symbol for p in got], ["AAPL"])
        self.assertEqual(got[0].bucket_id, 1)

    def test_close_refuses_cross_bucket(self):
        orders = [{"symbol": "TSLA", "client_order_id": "b2_TSLA_1"}]
        broker, _ = self._broker_with_orders(orders)
        # Bucket 1 does not own TSLA (bucket 2 does) -> refuse
        with self.assertRaises(AlpacaError):
            broker.close_position(1, "TSLA")

    def test_close_allows_owned(self):
        transport = MockTransport({
            ("GET", "/v2/orders"): [{"symbol": "AAPL", "client_order_id": "b1_AAPL_1"}],
            ("DELETE", "/v2/positions/AAPL"): {},
        })
        broker = AlpacaBrokerClient(KEYSTORE, transport=transport, clock=lambda: 1)
        broker.close_position(1, "AAPL")
        self.assertTrue(any(c[0] == "DELETE" for c in transport.calls))


class TestGetBars(unittest.TestCase):
    def test_parses_bars_and_uses_iex(self):
        transport = MockTransport({
            ("GET", "/v2/stocks/AAPL/bars"): {"bars": [
                {"t": "2026-07-10T14:30:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 5000},
            ]},
        })
        broker = AlpacaBrokerClient(KEYSTORE, transport=transport)
        bars = broker.get_bars("AAPL", "1Min", 10)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 100.5)
        self.assertEqual(bars[0].volume, 5000)
        self.assertIn("feed=iex", transport.calls[-1][1])


if __name__ == "__main__":
    unittest.main()
