"""Per-bucket capital accounting (Integration Spec Section 1 + 4).

Alpaca is a single account: buying power is shared, and the broker will happily
let one bucket spend another's cash. This ledger enforces per-bucket capital
ceilings *before* every order, in our own layer, so that leak can't happen.

It also computes portfolio heat (total open risk across all buckets) for the
loop's portfolio-level guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class BucketCapital:
    """One bucket's capital envelope and live usage."""

    bucket_id: int
    capital_ceiling: float          # max $ this bucket may deploy at once
    used: float = 0.0               # $ currently deployed (open positions)
    open_risk: float = 0.0          # $ at risk given stops (for portfolio heat)

    def available(self) -> float:
        return max(self.capital_ceiling - self.used, 0.0)


class BucketLedger:
    """Tracks each bucket's deployed capital and enforces its ceiling.

    The engine calls :meth:`can_deploy` before submitting an order and
    :meth:`on_open` / :meth:`on_close` as fills happen, so ``used`` reflects
    reality independently of Alpaca's shared account view.
    """

    def __init__(self, total_capital: float, portfolio_heat_cap_pct: float = 0.10) -> None:
        if total_capital <= 0:
            raise ValueError("total_capital must be > 0")
        self.total_capital = total_capital
        self.portfolio_heat_cap_pct = portfolio_heat_cap_pct
        self._buckets: Dict[int, BucketCapital] = {}

    def register_bucket(self, bucket_id: int, capital_ceiling: float) -> BucketCapital:
        cap = BucketCapital(bucket_id=bucket_id, capital_ceiling=capital_ceiling)
        self._buckets[bucket_id] = cap
        return cap

    def get(self, bucket_id: int) -> BucketCapital:
        if bucket_id not in self._buckets:
            raise KeyError(f"bucket {bucket_id} not registered in ledger")
        return self._buckets[bucket_id]

    # -- capital ceiling (the single-account fix) -------------------------
    def can_deploy(self, bucket_id: int, qty: float, price: float) -> bool:
        """True iff ``qty*price`` fits within this bucket's remaining ceiling."""
        notional = qty * price
        return notional <= self.get(bucket_id).available() + 1e-9

    def on_open(self, bucket_id: int, qty: float, price: float, risk: float = 0.0) -> None:
        cap = self.get(bucket_id)
        cap.used += qty * price
        cap.open_risk += risk

    def on_close(self, bucket_id: int, qty: float, price: float, risk: float = 0.0) -> None:
        cap = self.get(bucket_id)
        cap.used = max(cap.used - qty * price, 0.0)
        cap.open_risk = max(cap.open_risk - risk, 0.0)

    # -- portfolio heat ---------------------------------------------------
    def total_open_risk(self) -> float:
        return sum(c.open_risk for c in self._buckets.values())

    def portfolio_heat(self) -> float:
        """Total open risk as a fraction of total capital."""
        return self.total_open_risk() / self.total_capital

    def within_portfolio_heat(self, additional_risk: float = 0.0) -> bool:
        """True iff adding ``additional_risk`` keeps heat within the cap."""
        projected = (self.total_open_risk() + additional_risk) / self.total_capital
        return projected <= self.portfolio_heat_cap_pct + 1e-9
