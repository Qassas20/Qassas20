# Wiring the Three Integrations

Implements the Integration Spec — the broker adapter, the TA signal engine, the
Claude market-intelligence agent, and the orchestration loop that connects them
and feeds every fill into the [Reality Calibration Layer](CALIBRATION.md).

> **Language note.** The spec sketches the ports as Kotlin interfaces, but its
> instruction #4 is to *"feed every fill into the existing calibration layer's
> `raw_pnl`/`adjusted_pnl`"* — and that layer is this Python package. Writing the
> integrations in Kotlin would orphan it, so they're implemented in Python with
> the spec's port/adapter interfaces mirrored 1:1 (`BrokerClient`,
> `SignalEngine`, `MarketIntelligence`) — a later Kotlin Multiplatform port is
> mechanical. Real Alpaca/Claude calls live behind **injectable transports**, so
> everything is unit-tested with mocks and no network / no API keys.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  BACKEND (always-on)                                     │
│  MarketIntelligence (Claude, cached 5–15m) ─┐            │
│  SignalEngine (RSI/MACD/EMA — pure code) ───┼─▶ Loop ──▶ Alpaca (REST+WS)
│  BucketLedger (capital + portfolio heat) ───┘   │        │
│                                                  ▼        │
│                                    Reality Calibration Layer (SQLite)
└─────────────────────────────────────────────────────────┘
```

A minute-basis bot can't run in a phone's background (iOS/Android kill it) — the
engine belongs on an always-on backend; the mobile app is the control panel.

## 1. Broker adapter — `trading_bot/broker/`

`BrokerClient` (in `broker/base.py`) is the port the engine codes against;
`AlpacaBrokerClient` (`broker/alpaca.py`) implements it against Alpaca's REST +
WebSocket API (paper base URL, IEX feed).

**Alpaca is a single account** — buckets are a logical partition *you* enforce:

- **Per-bucket capital** is enforced by `BucketLedger` (`broker/accounting.py`)
  before every order, because Alpaca's buying power is shared and it will
  happily let one bucket spend another's cash.
- **Fills reconcile to buckets** via a `client_order_id` prefix
  (`b{bucket_id}_{symbol}_{epoch_ms}`); `parse_bucket_id` recovers it. Positions
  and closes are filtered/scoped by that prefix — `close_position` refuses a
  symbol the bucket doesn't own, so no cross-bucket close is possible.
- **Live keys** come from an injected keystore and are sent as Alpaca headers —
  never logged, never stored on the instance, never in the DB.

## 2. Signal engine — `trading_bot/signals/` (CODE, not an LLM)

`RuleSignalEngine` is deterministic computation over bars: EMA 9/21/50/200, RSI
14, MACD 12/26/9, volume confirmation, support/resistance (`signals/indicators.py`).
It emits a `Signal` only when multiple indicator groups align and sets
`confidence` from how many agree. This is exactly what a backtest runs against —
feed known bars, assert the signal. No Claude call is on this path (an LLM here
would be slow, costly, non-deterministic, and un-backtestable).

## 3. Market-intelligence agent — `trading_bot/intelligence/` (the real Claude call)

`ClaudeMarketIntelligence` runs backend-side, **not every minute**: the result
is cached and refreshed every N minutes (default 10), and every bucket reads the
cache. It pulls Alpaca news, sends a strict-JSON system prompt to Claude, and
**parses defensively — any failure defaults to NEUTRAL** so a down news/LLM
agent never blocks trading, it only removes the sentiment tilt. The returned
`MarketContext` modulates position sizing and a take/skip filter; it never places
trades.

- The Claude call uses the official `anthropic` SDK (`claude_client.py`),
  imported lazily so the package and tests run without it. Model defaults to
  `claude-opus-4-8`; set `AnthropicClaudeClient(model="claude-haiku-4-5")` if you
  want the cheaper tier for this high-frequency classifier.
- The `LLMClient` interface is `(system, user) -> str`, so tests inject a fake
  and never touch the network.

## 4. Orchestration loop — `trading_bot/orchestration/`

`TradingLoop.run_bucket_once` (`orchestration/loop.py`) runs per bucket per tick:

```
ctx = intelligence.assess(watchlist)          # cached, cheap
for symbol in bucket.watchlist:
    bars   = broker.get_bars(...)
    signal = signal_engine.evaluate(...)       # deterministic
    gate by confidence + regime + daily-loss    (risk.passes_risk)
    qty = size_position(ctx, signal, price)      (ctx modulates size)
    enforce bucket capital ceiling + portfolio heat (≤ 10%)
    submit bracket order (stop-loss + take-profit)
    record fill -> calibration raw_pnl / adjusted_pnl
enforce per-position stops + DAILY LOSS CUTOFF (pause this bucket only)
```

Guards enforced every tick: per-bucket **daily loss limit** (pauses only that
bucket), per-bucket **capital ceiling**, and **portfolio heat** (total open risk
across buckets ≤ 10% of total). Every close is scoped by `bucket_id` — never
account-wide.

`TradeRecorder` is the bridge to the calibration layer: on entry it models the
entry fill; on exit it models the exit fill, computes the round trip
(`model_round_trip`), tags the regime, and persists a `TradeRecord` with both
`raw_pnl` and `adjusted_pnl`. So the honest, friction-adjusted number remains the
one all reports and UAT gates use.

## Run it

```bash
python -m examples.integration_loop      # full loop -> calibration, no network
python -m unittest discover -s tests      # 139 tests, all mocked
```

`examples/integration_loop.py` runs the whole pipeline against a fake broker and
a fake Claude client and shows a round trip landing in `raw_pnl` / `adjusted_pnl`.

## Going live (later)

1. Implement the keystore to read live keys from env / an encrypted file — the
   adapter already keeps them out of logs and the DB.
2. Swap the fake broker for `AlpacaBrokerClient(keystore, paper=True)` and inject
   a real WebSocket `stream_source` for live bars.
3. Swap the fake Claude for `AnthropicClaudeClient()` (needs `pip install
   anthropic` and credentials).
4. Follow the calibration layer's shadow-live protocol before risking real money.
