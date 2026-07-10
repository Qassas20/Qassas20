# Backtest Harness

Measures whether the strategy has an edge — on `adjusted_pnl` — **before**
committing to the 4-week paper phase. Same deterministic `SignalEngine` the live
loop uses, same friction model from the [Reality Calibration Layer](CALIBRATION.md),
same metrics and UAT gates from the [reports layer](CALIBRATION.md).

## How it works

`Backtester.run` (in `trading_bot/backtest/engine.py`) walks historical bars
forward one at a time per symbol:

- **No lookahead** — the signal at bar *i* sees only bars `[0..i]`; entry
  executes at that bar's close, and friction (slippage + latency + liquidity cap)
  is applied on top. Stop/target levels are checked against *subsequent* bars.
- **Bracket exits** — stop-loss, take-profit, max-holding, or an opposite signal
  (all configurable via `BacktestConfig`).
- **Friction on every fill** — entry and exit both run through `model_fill` /
  `model_round_trip`, so each trade carries `raw_pnl` *and* `adjusted_pnl`.
- **Report + verdict** — aggregates into the standard `BucketReport` (expectancy,
  profit factor, drawdown, Sharpe, reality-gap metrics, per-regime split) and
  runs the UAT gate. Optionally persists every round trip to the `trades` table.

## Run it

```bash
python -m examples.run_backtest        # synthetic data, prints the verdict
```

```python
from trading_bot.backtest.engine import Backtester, BacktestConfig
from trading_bot.signals.engine import RuleSignalEngine
from trading_bot.calibration.config import CalibrationConfig

bt = Backtester(RuleSignalEngine(), CalibrationConfig(), BacktestConfig(warmup_bars=60))
result = bt.run({"AAPL": bars})     # bars: List[Bar]
print(result.summary())             # expectancy, PF, drawdown, regime split, UAT verdict
```

## Reading the output

On synthetic **random-walk** data (no real edge) the harness correctly declines:

```
trades=394  win_rate=36.3%  expectancy=1.60  PF=1.10  ...
raw_pnl=879.51  adjusted_pnl=630.90  friction_removed=248.61  ...
regime expectancy: trending=-8.08  choppy=3.41
UAT: INSUFFICIENT — Profit factor 1.10 < target 1.3; Trending-regime expectancy must be positive.
```

Note friction removed **28%** of the raw gain — that's the fake edge paper would
have shown. The UAT gate refusing a random-walk "strategy" is the system working
as designed.

## Feeding real Alpaca data

Swap synthetic bars for real historical IEX bars:

```python
from trading_bot.broker.alpaca import AlpacaBrokerClient

broker = AlpacaBrokerClient(keystore, paper=True)
bars = broker.get_bars("AAPL", "1Min", 1000)   # real IEX history
result = bt.run({"AAPL": bars})
```

A backtest can't replicate live microstructure, but with the friction layer
applied it is far closer to reality than raw paper — and it costs minutes, not
weeks. Trust `adjusted_pnl` expectancy split by regime over 100+ trades.
