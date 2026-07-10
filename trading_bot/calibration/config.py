"""Tunable calibration configuration for the Reality Calibration Layer.

Nothing in here is hardcoded into the friction math. Every constant lives in a
config object that is persisted to SQLite (``calibration_config`` table) and read
by the Trade Execution Agent at runtime, so it can be re-tuned against real live
fills during the shadow-live phase (see UAT addendum Section 6) without touching
code.

The regulatory fee rates in :class:`FeeSchedule` change over time. They MUST be
refreshed from Alpaca's / the SEC's / FINRA's published schedules rather than
trusted forever. The defaults below are only a conservative starting point.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


# ---------------------------------------------------------------------------
# Regulatory fees (Section 3.4)
# ---------------------------------------------------------------------------
# US equities are commission-free on Alpaca, but small regulatory fees apply on
# SELLS only. A minute/10-minute bot does many trades, so these are modeled.
# Rates change -> keep them in config, never hardcode in the math.
@dataclass(frozen=True)
class FeeSchedule:
    """Regulatory fee rates applied to the SELL side of a round trip.

    Attributes
    ----------
    sec_fee_per_dollar:
        SEC Section 31 fee, charged per dollar of sale *proceeds*. Historically
        quoted as ``$X per $1,000,000`` -> divide by 1e6 to get this value.
    finra_taf_per_share:
        FINRA Trading Activity Fee, charged per share sold.
    finra_taf_max:
        Per-trade cap on the FINRA TAF.
    """

    sec_fee_per_dollar: float = 0.0000278   # $27.80 per $1,000,000 of proceeds
    finra_taf_per_share: float = 0.000166   # per share sold
    finra_taf_max: float = 8.30             # per-trade cap

    def sell_fees(self, sell_proceeds: float, shares_sold: float) -> float:
        """Total regulatory fee for a sell of ``shares_sold`` at ``sell_proceeds``."""
        if sell_proceeds <= 0 or shares_sold <= 0:
            return 0.0
        sec = self.sec_fee_per_dollar * sell_proceeds
        taf = min(self.finra_taf_per_share * shares_sold, self.finra_taf_max)
        return sec + taf


# ---------------------------------------------------------------------------
# Calibration constants (Sections 3.1 - 3.3)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CalibrationConfig:
    """The tunable knobs of the friction model.

    Maps 1:1 onto the ``calibration_config`` SQLite table.
    """

    impact_k: float = 0.15            # market-impact constant, start ~0.1-0.3
    liquidity_cap_pct: float = 0.02   # cap fills at 2% of the minute bar volume
    latency_ms: int = 400             # signal -> fill delay (mobile/cellular)
    half_spread_min: float = 0.005    # always cross at least this many $/share
    fees: FeeSchedule = FeeSchedule()

    def __post_init__(self) -> None:
        if self.impact_k < 0:
            raise ValueError("impact_k must be >= 0")
        if not 0 < self.liquidity_cap_pct <= 1:
            raise ValueError("liquidity_cap_pct must be in (0, 1]")
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")
        if self.half_spread_min < 0:
            raise ValueError("half_spread_min must be >= 0")

    def with_updates(self, **kwargs) -> "CalibrationConfig":
        """Return a copy with the given fields replaced (configs are frozen)."""
        return replace(self, **kwargs)


DEFAULT_CONFIG = CalibrationConfig()
