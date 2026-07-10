"""UAT pass/fail/kill gates (UAT addendum Section 5).

Evaluates a bucket's *adjusted* track against the go-live criteria. A bucket
only graduates to (shadow) live if it PASSES every gate; it is explicitly KILLED
if it trips any kill criterion. Everything in between is INSUFFICIENT — not yet
enough evidence to decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Mapping, Optional, Sequence

from .metrics import core_metrics, regime_split

# Section 5 thresholds.
MIN_TRADES = 100                 # per bucket, before any number means anything
MIN_WEEKS = 4                    # duration; must span >=1 trending & >=1 choppy week
PROFIT_FACTOR_TARGET = 1.3
# A bot that only works in trends is a time bomb: choppy expectancy may be
# slightly negative but not worse than this fraction of trending expectancy.
CHOPPY_TOLERANCE = 0.0           # default: require choppy expectancy >= 0


class Verdict(str, Enum):
    PASS = "PASS"
    KILL = "KILL"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass
class UATResult:
    verdict: Verdict
    reasons: List[str] = field(default_factory=list)
    expectancy: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    trending_expectancy: float = 0.0
    choppy_expectancy: float = 0.0
    trade_count: int = 0

    @property
    def go_live(self) -> bool:
        return self.verdict is Verdict.PASS


def evaluate_bucket(
    trades: Sequence[Mapping],
    *,
    max_drawdown_limit: Optional[float] = None,
    weeks_observed: Optional[int] = None,
    saw_trending_week: bool = True,
    saw_choppy_week: bool = True,
    min_trades: int = MIN_TRADES,
    profit_factor_target: float = PROFIT_FACTOR_TARGET,
    choppy_tolerance: float = CHOPPY_TOLERANCE,
) -> UATResult:
    """Apply Section 5 gates to a bucket's adjusted track.

    Parameters mirror the addendum's duration/sample and pass/kill criteria.
    ``max_drawdown_limit`` is the bucket's risk-tier drawdown cap; if omitted the
    drawdown-breach kill check is skipped.
    """
    core = core_metrics(trades)
    regimes = regime_split(trades)
    trending_exp = regimes.trending.expectancy
    choppy_exp = regimes.choppy.expectancy

    result = UATResult(
        verdict=Verdict.INSUFFICIENT,
        expectancy=core.expectancy,
        profit_factor=core.profit_factor,
        max_drawdown=core.max_drawdown,
        trending_expectancy=trending_exp,
        choppy_expectancy=choppy_exp,
        trade_count=core.trade_count,
    )

    # --- KILL criteria (checked first; any one is disqualifying) ----------
    kills: List[str] = []
    if core.trade_count > 0 and core.expectancy <= 0:
        kills.append("Adjusted expectancy <= 0 (the edge was paper fantasy).")
    if max_drawdown_limit is not None and core.max_drawdown > max_drawdown_limit:
        kills.append(
            f"Max drawdown {core.max_drawdown:.2f} breaches bucket limit "
            f"{max_drawdown_limit:.2f}."
        )
    if kills:
        result.verdict = Verdict.KILL
        result.reasons = kills
        return result

    # --- Sufficiency gates (need enough evidence to pass) -----------------
    insufficient: List[str] = []
    if core.trade_count < min_trades:
        insufficient.append(
            f"Only {core.trade_count} trades; need >= {min_trades} for significance."
        )
    if weeks_observed is not None and weeks_observed < MIN_WEEKS:
        insufficient.append(f"Only {weeks_observed} weeks observed; need >= {MIN_WEEKS}.")
    if not (saw_trending_week and saw_choppy_week):
        insufficient.append(
            "Sample must span at least one trending week AND one choppy week."
        )

    # --- PASS criteria ----------------------------------------------------
    fails: List[str] = []
    if core.expectancy <= 0:
        fails.append("Expectancy must be positive.")
    if core.profit_factor < profit_factor_target:
        fails.append(
            f"Profit factor {core.profit_factor:.2f} < target {profit_factor_target}."
        )
    # Positive (or acceptably small negative) expectancy in BOTH regimes.
    if regimes.trending.trade_count > 0 and trending_exp <= 0:
        fails.append("Trending-regime expectancy must be positive.")
    if regimes.choppy.trade_count > 0 and choppy_exp < -abs(choppy_tolerance):
        fails.append(
            "Choppy-regime expectancy below tolerance (works only in trends)."
        )

    if insufficient:
        result.verdict = Verdict.INSUFFICIENT
        result.reasons = insufficient + fails
        return result
    if fails:
        # Enough data, but criteria not met -> not a pass. Treat as KILL-adjacent
        # "do not go live"; report as INSUFFICIENT edge with the failing reasons.
        result.verdict = Verdict.INSUFFICIENT
        result.reasons = fails
        return result

    result.verdict = Verdict.PASS
    result.reasons = ["All Section 5 gates satisfied on adjusted_pnl."]
    return result
