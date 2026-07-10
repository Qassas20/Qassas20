"""Unit tests for per-bucket capital accounting (Integration Spec §1 + §4)."""

import unittest

from trading_bot.broker.accounting import BucketLedger


class TestBucketLedger(unittest.TestCase):
    def setUp(self):
        self.ledger = BucketLedger(total_capital=100_000, portfolio_heat_cap_pct=0.10)
        self.ledger.register_bucket(1, capital_ceiling=10_000)
        self.ledger.register_bucket(2, capital_ceiling=5_000)

    def test_can_deploy_within_ceiling(self):
        self.assertTrue(self.ledger.can_deploy(1, qty=50, price=100))   # 5,000 < 10,000
        self.assertFalse(self.ledger.can_deploy(2, qty=100, price=100))  # 10,000 > 5,000

    def test_used_capital_tracks_opens(self):
        self.ledger.on_open(1, qty=50, price=100)  # 5,000 used
        self.assertAlmostEqual(self.ledger.get(1).used, 5_000)
        self.assertAlmostEqual(self.ledger.get(1).available(), 5_000)
        # Now only 5,000 headroom left
        self.assertFalse(self.ledger.can_deploy(1, qty=60, price=100))  # 6,000 > 5,000

    def test_close_frees_capital(self):
        self.ledger.on_open(1, qty=50, price=100)
        self.ledger.on_close(1, qty=50, price=100)
        self.assertAlmostEqual(self.ledger.get(1).used, 0.0)

    def test_one_bucket_cannot_use_anothers_capital(self):
        # Bucket 2 is capped at 5,000 regardless of bucket 1's unused room.
        self.assertFalse(self.ledger.can_deploy(2, qty=60, price=100))  # 6,000 > 5,000

    def test_portfolio_heat(self):
        self.ledger.on_open(1, qty=50, price=100, risk=6_000)
        self.ledger.on_open(2, qty=10, price=100, risk=3_000)
        # total risk 9,000 / 100,000 = 9% <= 10%
        self.assertAlmostEqual(self.ledger.portfolio_heat(), 0.09)
        self.assertTrue(self.ledger.within_portfolio_heat(500))    # -> 9.5%
        self.assertFalse(self.ledger.within_portfolio_heat(2_000))  # -> 11%

    def test_unregistered_bucket_raises(self):
        with self.assertRaises(KeyError):
            self.ledger.get(99)


if __name__ == "__main__":
    unittest.main()
