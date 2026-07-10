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
]
