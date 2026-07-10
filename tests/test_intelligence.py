"""Unit tests for the Market-Intelligence agent (Integration Spec §3).

Covers strict-JSON parsing, fence stripping, the default-to-neutral fallback,
and caching/refresh — all without touching the network or the Anthropic SDK.
"""

import unittest

from trading_bot.intelligence.market_intelligence import (
    ClaudeMarketIntelligence,
    MarketContext,
    Sentiment,
    VolLevel,
    _parse_context,
)


class FakeLLM:
    def __init__(self, reply="", raises=False):
        self.reply = reply
        self.raises = raises
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if self.raises:
            raise RuntimeError("LLM down")
        return self.reply


def no_news(symbols):
    return []


class MutableClock:
    def __init__(self, t=0):
        self.t = t

    def __call__(self):
        return self.t


class TestParsing(unittest.TestCase):
    def test_clean_json(self):
        raw = '{"sentiment":"bullish","volatility":"high","perSymbol":{"AAPL":"bearish"},"notes":"x"}'
        ctx = _parse_context(raw, ["AAPL"], now=1000)
        self.assertEqual(ctx.sentiment, Sentiment.BULLISH)
        self.assertEqual(ctx.volatility, VolLevel.HIGH)
        self.assertEqual(ctx.per_symbol["AAPL"], Sentiment.BEARISH)

    def test_strips_json_fences(self):
        raw = '```json\n{"sentiment":"bearish","volatility":"low","perSymbol":{},"notes":"y"}\n```'
        ctx = _parse_context(raw, ["AAPL"], now=1)
        self.assertEqual(ctx.sentiment, Sentiment.BEARISH)
        self.assertEqual(ctx.volatility, VolLevel.LOW)
        # Missing symbol defaults to neutral
        self.assertEqual(ctx.per_symbol["AAPL"], Sentiment.NEUTRAL)

    def test_prose_wrapped_json(self):
        raw = 'Here is the analysis: {"sentiment":"neutral","volatility":"medium","perSymbol":{},"notes":"z"} done'
        ctx = _parse_context(raw, [], now=1)
        self.assertEqual(ctx.sentiment, Sentiment.NEUTRAL)

    def test_garbage_defaults_neutral(self):
        ctx = _parse_context("not json at all", ["AAPL"], now=1)
        self.assertEqual(ctx.sentiment, Sentiment.NEUTRAL)
        self.assertEqual(ctx.volatility, VolLevel.MEDIUM)

    def test_bad_enum_values_coerced(self):
        raw = '{"sentiment":"very-bullish","volatility":"insane","perSymbol":{},"notes":""}'
        ctx = _parse_context(raw, [], now=1)
        self.assertEqual(ctx.sentiment, Sentiment.NEUTRAL)
        self.assertEqual(ctx.volatility, VolLevel.MEDIUM)


class TestFallback(unittest.TestCase):
    def test_llm_failure_defaults_neutral(self):
        mi = ClaudeMarketIntelligence(FakeLLM(raises=True), no_news, clock=MutableClock(5))
        ctx = mi.assess(["AAPL", "MSFT"])
        self.assertEqual(ctx.sentiment, Sentiment.NEUTRAL)
        self.assertEqual(ctx.per_symbol["AAPL"], Sentiment.NEUTRAL)

    def test_news_failure_defaults_neutral(self):
        def boom(symbols):
            raise RuntimeError("news down")
        mi = ClaudeMarketIntelligence(FakeLLM('{"sentiment":"bullish"}'), boom,
                                      clock=MutableClock(0))
        ctx = mi.assess(["AAPL"])
        self.assertEqual(ctx.sentiment, Sentiment.NEUTRAL)


class TestCaching(unittest.TestCase):
    def test_caches_within_ttl(self):
        llm = FakeLLM('{"sentiment":"bullish","volatility":"low","perSymbol":{},"notes":""}')
        clock = MutableClock(0)
        mi = ClaudeMarketIntelligence(llm, no_news, refresh_seconds=600, clock=clock)
        mi.assess(["AAPL"])
        clock.t = 300_000  # 5 min < 10 min TTL
        mi.assess(["AAPL"])
        self.assertEqual(llm.calls, 1)  # served from cache

    def test_refreshes_after_ttl(self):
        llm = FakeLLM('{"sentiment":"bullish","volatility":"low","perSymbol":{},"notes":""}')
        clock = MutableClock(0)
        mi = ClaudeMarketIntelligence(llm, no_news, refresh_seconds=600, clock=clock)
        mi.assess(["AAPL"])
        clock.t = 601_000  # past TTL
        mi.assess(["AAPL"])
        self.assertEqual(llm.calls, 2)


if __name__ == "__main__":
    unittest.main()
