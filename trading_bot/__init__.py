"""Reality Calibration Layer for the multi-bucket trading bot.

Turns Alpaca's optimistic paper-trading numbers into an honest predictor of live
performance. See ``docs/CALIBRATION.md`` and the UAT addendum for the full model.

Top-level convenience exports:

    from trading_bot import (
        CalibrationConfig, model_fill, model_round_trip,
        CalibrationDB, TradeRecord, bucket_report, evaluate_bucket,
        AlpacaExecutionAgent, BucketScope,
    )
"""

from .calibration.config import CalibrationConfig, FeeSchedule, DEFAULT_CONFIG
from .calibration.friction import (
    model_fill,
    model_round_trip,
    FillFriction,
    TradeFriction,
)
from .calibration.regime import ma_slope_regime, adx_regime, compute_adx
from .calibration import shadow
from .db.database import CalibrationDB, TradeRecord
from .reporting.metrics import (
    bucket_report,
    core_metrics,
    reality_gap_metrics,
    regime_split,
    forecast,
    expectancy,
    profit_factor,
)
from .reporting.uat import evaluate_bucket, Verdict, UATResult
from .execution.alpaca_cli import AlpacaExecutionAgent, BucketScope, ExecutionError

# --- Live-engine integrations (Integration Spec) ---
from .broker.base import (
    BrokerClient,
    Bar,
    Account,
    Position,
    Order,
    Side,
    OrderType,
    make_client_order_id,
    parse_bucket_id,
)
from .broker.accounting import BucketLedger, BucketCapital
from .broker.alpaca import AlpacaBrokerClient
from .signals.engine import SignalEngine, RuleSignalEngine, Signal, RuleConfig
from .intelligence.market_intelligence import (
    MarketIntelligence,
    ClaudeMarketIntelligence,
    MarketContext,
    Sentiment,
    VolLevel,
)
from .intelligence.claude_client import AnthropicClaudeClient, LLMClient
from .intelligence.news import AlpacaNewsClient
from .orchestration.loop import TradingLoop, TradeRecorder, Bucket
from .orchestration import risk as risk
from .backtest.engine import Backtester, BacktestConfig, BacktestResult

__all__ = [
    "CalibrationConfig",
    "FeeSchedule",
    "DEFAULT_CONFIG",
    "model_fill",
    "model_round_trip",
    "FillFriction",
    "TradeFriction",
    "ma_slope_regime",
    "adx_regime",
    "compute_adx",
    "shadow",
    "CalibrationDB",
    "TradeRecord",
    "bucket_report",
    "core_metrics",
    "reality_gap_metrics",
    "regime_split",
    "forecast",
    "expectancy",
    "profit_factor",
    "evaluate_bucket",
    "Verdict",
    "UATResult",
    "AlpacaExecutionAgent",
    "BucketScope",
    "ExecutionError",
    # integrations
    "BrokerClient",
    "Bar",
    "Account",
    "Position",
    "Order",
    "Side",
    "OrderType",
    "make_client_order_id",
    "parse_bucket_id",
    "BucketLedger",
    "BucketCapital",
    "AlpacaBrokerClient",
    "SignalEngine",
    "RuleSignalEngine",
    "Signal",
    "RuleConfig",
    "MarketIntelligence",
    "ClaudeMarketIntelligence",
    "MarketContext",
    "Sentiment",
    "VolLevel",
    "AnthropicClaudeClient",
    "LLMClient",
    "AlpacaNewsClient",
    "TradingLoop",
    "TradeRecorder",
    "Bucket",
    "risk",
    "Backtester",
    "BacktestConfig",
    "BacktestResult",
]
