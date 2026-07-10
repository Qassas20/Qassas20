# Reality Calibration Layer

Makes the Alpaca paper-trading phase a **valid predictor** of live performance
instead of an optimistic mirage. This implements the UAT & Reality Calibration
addendum (Phase 5/7) for the multi-bucket trading bot.

> **Core problem:** Alpaca paper trading *overstates* results — it fills orders
> larger than real liquidity, models no slippage, and injects no latency.
> Whatever paper shows, live will be worse. This layer closes that gap by
> recording two P&L tracks per trade and trusting only the friction-adjusted one.

```
signal ─▶ friction model ─▶ raw_pnl + adjusted_pnl ─▶ reports ─▶ UAT gate ─▶ shadow-live
          (slippage,          (SQLite)                 (adjusted   (go/no-go)   (calibrate
           liquidity cap,                                only)                   k, latency)
           latency, fees)
```

## Layout

| Module | Responsibility | Addendum § |
|---|---|---|
| `trading_bot/calibration/config.py` | Tunable constants + fee schedule (never hardcoded) | 3.1–3.4 |
| `trading_bot/calibration/friction.py` | Slippage, liquidity cap, latency, round-trip P&L | 3.1–3.3 |
| `trading_bot/calibration/regime.py` | Trending/choppy tagging (MA-slope, ADX) | 3.5 |
| `trading_bot/calibration/shadow.py` | Paper-vs-live gap analysis + tuning hints | 6 |
| `trading_bot/db/schema.sql` + `database.py` | SQLite persistence of both P&L tracks | 7 |
| `trading_bot/reporting/metrics.py` | Core + reality-gap metrics, forecasts | 4 |
| `trading_bot/reporting/uat.py` | Pass / kill / insufficient gates | 5 |
| `trading_bot/execution/alpaca_cli.py` | Bucket-scoped Trade Execution Agent | 1 |

## The friction model

For each fill we deduct, in order:

1. **Liquidity cap** (§3.2) — haircut any fill exceeding `liquidity_cap_pct` of
   the minute bar's volume; the rejected remainder is logged as `haircut_qty`.
   Fixes paper's biggest lie: filling more than real liquidity.
2. **Latency** (§3.3) — re-price the fill at `signal_time + latency_ms` instead
   of the signal timestamp.
3. **Slippage** (§3.1) — deduct `half_spread + market_impact` per share, where
   `market_impact = k · (order_qty / bar_volume)`. We always cross at least
   `half_spread_min`. Slippage always works against you: buys fill higher, sells
   fill lower.
4. **Fees** (§3.4) — SEC Section 31 fee + FINRA TAF on the sell side, read from
   config (rates change — refresh them from the published schedules).

`raw_pnl` uses the frictionless signal prices (what Alpaca reports).
`adjusted_pnl` is after all of the above. **All reports and forecasts use
`adjusted_pnl` only.**

## Quick start

```python
from trading_bot import (
    CalibrationConfig, CalibrationDB, TradeRecord,
    model_fill, model_round_trip, bucket_report, evaluate_bucket,
)

config = CalibrationConfig(impact_k=0.15, liquidity_cap_pct=0.02, latency_ms=400)
db = CalibrationDB("calibration.db")
bucket = db.add_bucket("momentum", risk_tier="high", max_drawdown=2000)

entry = model_fill("buy", 200, signal_time_ms=0,      signal_price=100.0,
                   spread=0.02, bar_volume=50_000, config=config)
exit  = model_fill("sell", 200, signal_time_ms=60_000, signal_price=100.6,
                   spread=0.02, bar_volume=50_000, config=config)
trade = model_round_trip(entry, exit, config, direction="long")

db.insert_trade(TradeRecord.from_friction(bucket, "AAPL", 200, trade, regime="trending"))

report = bucket_report(db.trades_for_bucket(bucket))   # everything off adjusted_pnl
gate   = evaluate_bucket(db.trades_for_bucket(bucket),
                         max_drawdown_limit=2000, weeks_observed=4)
print(gate.verdict, gate.go_live)
```

See `examples/end_to_end.py` for a full synthetic session.

## UAT gates (§5)

A bucket graduates to (shadow) live only when, on `adjusted_pnl`:

- ≥ 100 trades over ≥ 4 weeks spanning **both** a trending and a choppy week,
- positive expectancy per trade,
- profit factor > 1.3,
- max drawdown within the bucket's risk-tier limit,
- positive (or acceptably small negative) expectancy in **both** regimes.

`evaluate_bucket` returns `PASS`, `KILL` (negative expectancy or drawdown breach
— the edge was paper fantasy), or `INSUFFICIENT` (not enough evidence / criteria
not yet met).

## Trade Execution Agent safety (§1)

The Alpaca CLI has **no guardrails** — trade commands execute immediately and
`order cancel-all` / `position close-all` act account-wide with no confirmation.
`AlpacaExecutionAgent` therefore:

- never exposes an account-wide bulk op; `cancel_all_scoped` / `close_all_scoped`
  loop over **only this bucket's symbols**, so one bucket's panic-close can't
  nuke another bucket's positions;
- refuses any order/quote on a symbol outside the bucket's `BucketScope`;
- reads live API keys from an injected keystore only — never logs them, never
  puts them in argv, the repo, or the DB (paper uses OAuth, no keys).

## Paper → live transition (§6)

Run paper and live in parallel on tiny capital, log each fill's
`paper_adjusted_pnl` vs `live_actual_pnl` (`live_vs_paper` table), then use
`shadow.summarize()` / `shadow.calibration_hint()` to tune `impact_k` and
`latency_ms` until adjusted-paper tracks real live P&L. Keep the paper twin
running forever — divergence is an early warning that conditions changed.

## Tests

```bash
python -m unittest discover -s tests -v
```

Friction math is covered exhaustively in `tests/test_friction.py`.
