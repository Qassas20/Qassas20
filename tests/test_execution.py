"""Unit tests for the bucket-scoped Trade Execution Agent (Section 1)."""

import unittest

from trading_bot.execution.alpaca_cli import (
    AlpacaExecutionAgent,
    BucketScope,
    ExecutionError,
)


class FakeRunner:
    """Records argv and returns canned responses keyed by command."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        # key on the two command tokens after the binary
        key = tuple(argv[1:3])
        return self.responses.get(key, {})


class TestScopingGuards(unittest.TestCase):
    def setUp(self):
        self.scope = BucketScope(bucket_id=1, symbols=["AAPL", "MSFT"])
        self.runner = FakeRunner()
        self.agent = AlpacaExecutionAgent(self.scope, runner=self.runner)

    def test_submit_in_scope_ok(self):
        self.agent.submit_order("AAPL", "buy", 10)
        self.assertEqual(self.runner.calls[-1][:2], ["alpaca", "order"])
        self.assertIn("AAPL", self.runner.calls[-1])

    def test_submit_out_of_scope_refused(self):
        with self.assertRaises(ExecutionError):
            self.agent.submit_order("TSLA", "buy", 10)
        # nothing was executed
        self.assertEqual(self.runner.calls, [])

    def test_quote_out_of_scope_refused(self):
        with self.assertRaises(ExecutionError):
            self.agent.quote("TSLA")

    def test_bad_side_rejected(self):
        with self.assertRaises(ValueError):
            self.agent.submit_order("AAPL", "hodl", 10)


class TestBucketScopedBulkOps(unittest.TestCase):
    def test_close_all_only_touches_own_symbols(self):
        # position list returns positions across MANY symbols; only ours close.
        runner = FakeRunner({
            ("position", "list"): [
                {"symbol": "AAPL", "qty": 10},
                {"symbol": "TSLA", "qty": 5},    # belongs to another bucket!
                {"symbol": "MSFT", "qty": 3},
            ],
        })
        scope = BucketScope(1, ["AAPL", "MSFT"])
        agent = AlpacaExecutionAgent(scope, runner=runner)
        agent.close_all_scoped()
        closed = [c for c in runner.calls if c[1:3] == ["position", "close"]]
        closed_symbols = {c[c.index("--symbol") + 1] for c in closed}
        self.assertEqual(closed_symbols, {"AAPL", "MSFT"})
        self.assertNotIn("TSLA", closed_symbols)

    def test_cancel_all_scoped_filters(self):
        runner = FakeRunner({
            ("order", "list"): [
                {"id": "1", "symbol": "AAPL"},
                {"id": "2", "symbol": "TSLA"},
                {"id": "3", "symbol": "MSFT"},
            ],
        })
        scope = BucketScope(1, ["AAPL", "MSFT"])
        agent = AlpacaExecutionAgent(scope, runner=runner)
        agent.cancel_all_scoped()
        cancels = [c for c in runner.calls if c[1:3] == ["order", "cancel"]]
        cancelled_ids = {c[c.index("--order-id") + 1] for c in cancels}
        self.assertEqual(cancelled_ids, {"1", "3"})  # never "2" (TSLA)

    def test_positions_filtered_to_scope(self):
        runner = FakeRunner({
            ("position", "list"): [
                {"symbol": "AAPL", "qty": 10},
                {"symbol": "TSLA", "qty": 5},
            ],
        })
        scope = BucketScope(1, ["AAPL"])
        agent = AlpacaExecutionAgent(scope, runner=runner)
        positions = agent.positions()
        self.assertEqual([p["symbol"] for p in positions], ["AAPL"])


class TestLiveMode(unittest.TestCase):
    def test_live_requires_keystore(self):
        with self.assertRaises(ExecutionError):
            AlpacaExecutionAgent(BucketScope(1, ["AAPL"]), mode="live")

    def test_live_with_keystore_ok(self):
        agent = AlpacaExecutionAgent(
            BucketScope(1, ["AAPL"]),
            mode="live",
            runner=FakeRunner(),
            keystore=lambda: {"ALPACA_API_KEY_ID": "x", "ALPACA_API_SECRET_KEY": "y"},
        )
        self.assertEqual(agent.mode, "live")

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            AlpacaExecutionAgent(BucketScope(1, ["AAPL"]), mode="sim")


if __name__ == "__main__":
    unittest.main()
