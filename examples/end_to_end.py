"""End-to-end demo of the Reality Calibration Layer.

Runs a small synthetic paper session through the whole pipeline:

    signal -> friction (slippage + liquidity cap + latency + fees)
           -> raw_pnl & adjusted_pnl in SQLite
           -> reports (from adjusted_pnl only)
           -> UAT go/no-go gate
           -> shadow-live gap analysis

Run: ``python -m examples.end_to_end``
"""

from __future__ import annotations

from trading_bot import (
    CalibrationConfig,
    FeeSchedule,
    CalibrationDB,
    TradeRecord,
    model_fill,
    model_round_trip,
    bucket_report,
    evaluate_bucket,
    shadow,
)


def run() -> None:
    config = CalibrationConfig(
        impact_k=0.15,
        liquidity_cap_pct=0.02,
        latency_ms=400,
        half_spread_min=0.005,
        fees=FeeSchedule(),
    )

    db = CalibrationDB(":memory:")
    db.save_config(config, updated_at="2026-07-10T00:00:00Z")
    bucket = db.add_bucket("momentum", risk_tier="high", max_drawdown=2_000, daily_stop=500)

    # A synthetic edge: entries at 100, exits mostly a bit higher. Regimes vary.
    session = []
    for i in range(120):
        regime = "trending" if i % 3 else "choppy"
        # trending trades win more; choppy roughly breaks even
        move = 0.6 if regime == "trending" else 0.15
        entry_px, exit_px = 100.0, 100.0 + move
        session.append((entry_px, exit_px, regime))

    for entry_px, exit_px, regime in session:
        entry = model_fill("buy", 200, 0, entry_px, spread=0.02,
                           bar_volume=50_000, config=config)
        exit = model_fill("sell", 200, 60_000, exit_px, spread=0.02,
                          bar_volume=50_000, config=config)
        tr = model_round_trip(entry, exit, config, direction="long")
        db.insert_trade(
            TradeRecord.from_friction(bucket, "AAPL", min(entry.filled_qty, exit.filled_qty),
                                      tr, regime=regime)
        )

    rows = db.trades_for_bucket(bucket)
    report = bucket_report(rows)

    print("=== CORE (adjusted_pnl) ===")
    print(f"trades={report.core.trade_count} win_rate={report.core.win_rate:.2%} "
          f"expectancy={report.core.expectancy:.4f} PF={report.core.profit_factor:.2f} "
          f"maxDD={report.core.max_drawdown:.2f}")

    print("\n=== REALITY GAP ===")
    g = report.reality_gap
    print(f"raw={g.total_raw_pnl:.2f} adjusted={g.total_adjusted_pnl:.2f} "
          f"gap={g.raw_vs_adjusted_gap:.2f} | slippage/trade={g.modeled_slippage_per_trade:.4f} "
          f"fees={g.total_fees:.2f} haircut_qty={g.haircut_qty_total:.0f} "
          f"fill_drift={g.avg_fill_drift_bps:.1f}bps")

    print("\n=== REGIME SPLIT (expectancy) ===")
    print(f"trending={report.regimes.trending.expectancy:.4f} "
          f"choppy={report.regimes.choppy.expectancy:.4f}")

    print("\n=== UAT GATE ===")
    verdict = evaluate_bucket(rows, max_drawdown_limit=2_000, weeks_observed=4,
                              saw_trending_week=True, saw_choppy_week=True)
    print(f"verdict={verdict.verdict.value} go_live={verdict.go_live}")
    for r in verdict.reasons:
        print(f"  - {r}")

    print("\n=== SHADOW-LIVE (paper vs live) ===")
    # Pretend live came in a touch worse than adjusted-paper predicted.
    for r in rows[:20]:
        paper = r["adjusted_pnl"]
        db.log_live_vs_paper(bucket, "AAPL", "2026-07-10T10:00:00Z",
                             paper_adjusted_pnl=paper, live_actual_pnl=paper * 0.9)
    summary = shadow.summarize(db.live_vs_paper_rows(bucket))
    print(f"n={summary.n} mean_gap={summary.mean_gap:.4f} direction={summary.direction}")
    print(f"  hint: {shadow.calibration_hint(summary)}")

    db.close()


if __name__ == "__main__":
    run()
