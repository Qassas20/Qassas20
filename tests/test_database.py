"""Unit tests for SQLite persistence (UAT addendum Section 7)."""

import unittest

from trading_bot.calibration.config import CalibrationConfig, FeeSchedule
from trading_bot.calibration import friction as F
from trading_bot.db.database import CalibrationDB, TradeRecord


class TestSchema(unittest.TestCase):
    def test_tables_exist(self):
        with CalibrationDB() as db:
            rows = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = {r["name"] for r in rows}
            for expected in ("buckets", "trades", "calibration_config", "live_vs_paper"):
                self.assertIn(expected, names)

    def test_trade_columns_present(self):
        with CalibrationDB() as db:
            cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(trades)")}
            for c in ("raw_pnl", "adjusted_pnl", "modeled_slippage", "fees",
                      "latency_ms", "haircut_qty", "regime", "signal_price",
                      "modeled_fill_price"):
                self.assertIn(c, cols)


class TestTradePersistence(unittest.TestCase):
    def test_insert_and_read_both_pnl_tracks(self):
        with CalibrationDB() as db:
            bucket = db.add_bucket("momentum", risk_tier="high", max_drawdown=500)
            rec = TradeRecord(
                bucket_id=bucket, symbol="AAPL", qty=100, raw_pnl=100.0,
                adjusted_pnl=97.5, modeled_slippage=0.02, fees=0.5, latency_ms=400,
                haircut_qty=10, regime="trending", signal_price=50.0,
                modeled_fill_price=50.01,
            )
            db.insert_trade(rec)
            rows = db.trades_for_bucket(bucket)
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["raw_pnl"], 100.0)
            self.assertAlmostEqual(rows[0]["adjusted_pnl"], 97.5)
            self.assertEqual(rows[0]["regime"], "trending")

    def test_from_friction_roundtrip(self):
        cfg = CalibrationConfig(impact_k=0.0, liquidity_cap_pct=1.0, latency_ms=0,
                                half_spread_min=0.01,
                                fees=FeeSchedule(0.0, 0.0, 0.0))
        entry = F.model_fill("buy", 100, 0, 50.0, 0.02, 1_000_000, cfg)
        exit = F.model_fill("sell", 100, 0, 51.0, 0.02, 1_000_000, cfg)
        tr = F.model_round_trip(entry, exit, cfg)
        with CalibrationDB() as db:
            bucket = db.add_bucket("b")
            rec = TradeRecord.from_friction(bucket, "AAPL", 100, tr, regime="trending")
            db.insert_trade(rec)
            row = db.trades_for_bucket(bucket)[0]
            self.assertAlmostEqual(row["raw_pnl"], 100.0)
            self.assertAlmostEqual(row["adjusted_pnl"], 98.0)


class TestConfigPersistence(unittest.TestCase):
    def test_save_and_load_latest(self):
        with CalibrationDB() as db:
            self.assertIsNone(db.latest_config())
            cfg = CalibrationConfig(impact_k=0.25, liquidity_cap_pct=0.03,
                                    latency_ms=500, half_spread_min=0.01)
            db.save_config(cfg, updated_at="2026-07-10")
            loaded = db.latest_config()
            self.assertAlmostEqual(loaded.impact_k, 0.25)
            self.assertAlmostEqual(loaded.liquidity_cap_pct, 0.03)
            self.assertEqual(loaded.latency_ms, 500)

    def test_latest_wins(self):
        with CalibrationDB() as db:
            db.save_config(CalibrationConfig(impact_k=0.1))
            db.save_config(CalibrationConfig(impact_k=0.3))
            self.assertAlmostEqual(db.latest_config().impact_k, 0.3)


class TestLiveVsPaper(unittest.TestCase):
    def test_gap_computed(self):
        with CalibrationDB() as db:
            bucket = db.add_bucket("b")
            db.log_live_vs_paper(bucket, "AAPL", "2026-07-10T10:00", 100.0, 92.0)
            rows = db.live_vs_paper_rows(bucket)
            self.assertEqual(len(rows), 1)
            # gap = live - paper = 92 - 100 = -8 (live worse, as expected)
            self.assertAlmostEqual(rows[0]["gap"], -8.0)


if __name__ == "__main__":
    unittest.main()
