"""Market-Intelligence agent (Integration Spec §3) — the one real Claude call.

Runs backend-side, NOT every minute. News sentiment moves on minutes-to-hours,
so the result is cached and refreshed every N minutes; every bucket reads the
cache. Output is strict JSON, parsed defensively — **on any failure it defaults
to NEUTRAL** so a down news agent never blocks trading, it only removes the
sentiment tilt.

What it modulates (never per-trade, never places orders): the loop uses the
returned :class:`MarketContext` to size positions and to skip low-confidence
signals in bad conditions.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from .claude_client import LLMClient
from .news import Headline


class Sentiment(str, Enum):
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"


class VolLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class MarketContext:
    sentiment: Sentiment
    volatility: VolLevel
    per_symbol: Dict[str, Sentiment]
    notes: str
    timestamp_ms: int

    @staticmethod
    def neutral(timestamp_ms: int, symbols: Optional[List[str]] = None) -> "MarketContext":
        per = {s: Sentiment.NEUTRAL for s in (symbols or [])}
        return MarketContext(Sentiment.NEUTRAL, VolLevel.MEDIUM, per,
                             "defaulted to neutral", timestamp_ms)


class MarketIntelligence(ABC):
    @abstractmethod
    def assess(self, symbols: List[str]) -> MarketContext:
        """Return the (cached) MarketContext for the watchlist."""


_SYSTEM_PROMPT = (
    "You are a market sentiment analyzer. Given headlines and the economic "
    "calendar, return ONLY valid JSON, no preamble, no markdown:\n"
    '{ "sentiment": "bullish|neutral|bearish",\n'
    '  "volatility": "low|medium|high",\n'
    '  "perSymbol": { "AAPL": "neutral" },\n'
    '  "notes": "one line" }'
)


class ClaudeMarketIntelligence(MarketIntelligence):
    """Cached, backend-side Claude sentiment assessor.

    Parameters
    ----------
    llm:
        Any :class:`LLMClient` (real Anthropic client or a fake in tests).
    news_fetch:
        Callable ``symbols -> List[Headline]`` (e.g. ``AlpacaNewsClient.recent``).
    refresh_seconds:
        Cache TTL. Within it, ``assess`` returns the cached context without
        calling Claude — keeping cost and latency sane across many buckets.
    clock:
        Injectable ``() -> epoch_ms`` for deterministic cache tests.
    """

    def __init__(
        self,
        llm: LLMClient,
        news_fetch: Callable[[List[str]], List[Headline]],
        refresh_seconds: int = 600,          # 10 min, within the 5-15 min guidance
        clock: Optional[Callable[[], int]] = None,
        calendar_fetch: Optional[Callable[[List[str]], str]] = None,
    ) -> None:
        self._llm = llm
        self._news_fetch = news_fetch
        self._calendar_fetch = calendar_fetch
        self.refresh_ms = refresh_seconds * 1000
        self._clock = clock or _default_clock
        self._cache: Optional[MarketContext] = None

    def assess(self, symbols: List[str]) -> MarketContext:
        now = self._clock()
        if self._cache is not None and now - self._cache.timestamp_ms < self.refresh_ms:
            return self._cache
        ctx = self._refresh(symbols, now)
        self._cache = ctx
        return ctx

    def _refresh(self, symbols: List[str], now: int) -> MarketContext:
        try:
            headlines = self._news_fetch(symbols)
            calendar = self._calendar_fetch(symbols) if self._calendar_fetch else ""
            user = _build_user_prompt(symbols, headlines, calendar)
            raw = self._llm.complete(_SYSTEM_PROMPT, user)
            return _parse_context(raw, symbols, now)
        except Exception:
            # A down news/LLM agent must never block trading — go neutral.
            return MarketContext.neutral(now, symbols)


# ---------------------------------------------------------------------------
# Defensive JSON parsing — strip fences, json.loads, catch -> neutral
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _parse_context(raw: str, symbols: List[str], now: int) -> MarketContext:
    """Parse strict JSON defensively; any failure -> neutral MarketContext."""
    try:
        cleaned = _FENCE_RE.sub("", raw.strip())
        # If the model wrapped JSON in prose, grab the first {...} block.
        if not cleaned.startswith("{"):
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            cleaned = match.group(0) if match else cleaned
        data = json.loads(cleaned)
        sentiment = _coerce_sentiment(data.get("sentiment"))
        volatility = _coerce_vol(data.get("volatility"))
        per_raw = data.get("perSymbol") or {}
        per = {s: _coerce_sentiment(per_raw.get(s)) for s in symbols}
        notes = str(data.get("notes", ""))[:280]
        return MarketContext(sentiment, volatility, per, notes, now)
    except Exception:
        return MarketContext.neutral(now, symbols)


def _coerce_sentiment(val) -> Sentiment:
    try:
        return Sentiment(str(val).strip().lower())
    except (ValueError, AttributeError):
        return Sentiment.NEUTRAL


def _coerce_vol(val) -> VolLevel:
    try:
        return VolLevel(str(val).strip().lower())
    except (ValueError, AttributeError):
        return VolLevel.MEDIUM


def _build_user_prompt(symbols: List[str], headlines: List[Headline], calendar: str) -> str:
    lines = [f"Watchlist: {', '.join(symbols)}", "", "Recent headlines:"]
    if headlines:
        for h in headlines[:40]:
            tag = ",".join(h.symbols) if h.symbols else "-"
            lines.append(f"- [{tag}] {h.headline}")
    else:
        lines.append("- (none)")
    if calendar:
        lines += ["", "Economic calendar:", calendar]
    return "\n".join(lines)


def _default_clock() -> int:
    import time
    return int(time.time() * 1000)
