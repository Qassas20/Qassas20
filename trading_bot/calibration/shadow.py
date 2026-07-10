"""Shadow-live gap analysis (UAT addendum Section 6).

During the paper->live transition you run paper and live in parallel on tiny
capital and log, per fill, ``paper_adjusted_pnl`` vs ``live_actual_pnl`` (into the
``live_vs_paper`` table). This module summarizes that gap and gives a coarse,
directional hint on whether the friction model is too optimistic or too harsh —
the signal you use to calibrate ``impact_k`` / ``latency_ms`` until paper
adjusted P&L tracks real live P&L.

It deliberately does NOT auto-tune constants: calibration is a human-in-the-loop
decision. It quantifies the divergence and flags the direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Sequence


@dataclass
class GapSummary:
    n: int = 0
    mean_gap: float = 0.0              # mean(live_actual - paper_adjusted)
    mean_abs_gap: float = 0.0
    cumulative_gap: float = 0.0
    mean_paper: float = 0.0
    mean_live: float = 0.0
    direction: str = "aligned"        # 'model_optimistic' | 'model_pessimistic' | 'aligned'


def _val(row: Mapping, key: str) -> float:
    v = row[key]
    return 0.0 if v is None else v


def summarize(rows: Sequence[Mapping], tolerance: float = 0.05) -> GapSummary:
    """Summarize paper-vs-live divergence.

    Parameters
    ----------
    rows:
        ``live_vs_paper`` rows (sqlite3.Row or dicts) exposing
        ``paper_adjusted_pnl``, ``live_actual_pnl`` and ``gap``.
    tolerance:
        Fraction of the paper magnitude within which the model is "honest".
        Used only to classify ``direction`` (not a hard gate).
    """
    n = len(rows)
    s = GapSummary(n=n)
    if n == 0:
        return s

    papers = [_val(r, "paper_adjusted_pnl") for r in rows]
    lives = [_val(r, "live_actual_pnl") for r in rows]
    gaps = [live - paper for paper, live in zip(papers, lives)]

    s.mean_paper = sum(papers) / n
    s.mean_live = sum(lives) / n
    s.mean_gap = sum(gaps) / n
    s.mean_abs_gap = sum(abs(g) for g in gaps) / n
    s.cumulative_gap = sum(gaps)

    scale = max(abs(s.mean_paper), 1e-9)
    if s.mean_gap < -tolerance * scale:
        # live came in WORSE than paper predicted -> model still too optimistic.
        s.direction = "model_optimistic"
    elif s.mean_gap > tolerance * scale:
        # live BETTER than modeled -> friction is too harsh.
        s.direction = "model_pessimistic"
    else:
        s.direction = "aligned"
    return s


def calibration_hint(summary: GapSummary) -> str:
    """Plain-English next step for tuning the constants."""
    if summary.n == 0:
        return "No shadow-live data yet — run paper and live in parallel first."
    if summary.direction == "model_optimistic":
        return (
            "Live underperformed paper-adjusted: increase impact_k and/or "
            "latency_ms so adjusted_pnl comes down toward live."
        )
    if summary.direction == "model_pessimistic":
        return (
            "Live outperformed paper-adjusted: friction is too harsh — lower "
            "impact_k and/or latency_ms."
        )
    return "Model is tracking live within tolerance — hold constants, keep monitoring."
