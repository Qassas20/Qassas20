"""Claude client for the Market-Intelligence agent (Integration Spec §3).

A thin wrapper over the official Anthropic SDK (`anthropic`), imported lazily so
the rest of the package works — and tests run — without it installed. The
sentiment classifier is a cheap, cached, backend-side call, so it uses a low
effort setting and no extended thinking (headline sentiment is a simple
classification, not a reasoning task).

The callable shape is intentionally minimal — ``(system, user) -> str`` — so
`MarketIntelligence` can be handed a fake in tests and never touch the network.
"""

from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
    """Anything that turns a (system, user) pair into raw response text."""

    def complete(self, system: str, user: str) -> str: ...


# Default model: the skill guidance is to use claude-opus-4-8 unless a cheaper
# tier is explicitly chosen. It's a config field so a cost-sensitive operator can
# switch to claude-haiku-4-5 for this high-frequency classifier if they want.
DEFAULT_MODEL = "claude-opus-4-8"


class AnthropicClaudeClient:
    """Real Claude client backed by the Anthropic SDK.

    Parameters
    ----------
    model:
        Model id. Defaults to ``claude-opus-4-8``.
    api_key:
        Optional explicit key. If omitted, the SDK resolves credentials from the
        environment / an ``ant auth login`` profile — no key is hardcoded here.
    max_tokens / effort:
        Tuned for a fast, cheap classifier. ``effort="low"`` keeps latency and
        cost down; thinking is left off (omitted) for the same reason.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        max_tokens: int = 512,
        effort: str = "low",
    ) -> None:
        self.model = model
        self._api_key = api_key
        self.max_tokens = max_tokens
        self.effort = effort
        self._client = None  # lazily constructed

    def _ensure_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise RuntimeError(
                    "the 'anthropic' package is required for AnthropicClaudeClient; "
                    "pip install anthropic (or inject a custom LLMClient)"
                ) from exc
            self._client = (
                anthropic.Anthropic(api_key=self._api_key)
                if self._api_key
                else anthropic.Anthropic()
            )
        return self._client

    def complete(self, system: str, user: str) -> str:
        client = self._ensure_client()
        resp = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            output_config={"effort": self.effort},
            messages=[{"role": "user", "content": user}],
        )
        # Concatenate text blocks; ignore any non-text blocks defensively.
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
