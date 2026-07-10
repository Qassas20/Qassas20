"""News source for the Market-Intelligence agent (Integration Spec §3).

Alpaca exposes a news endpoint via the data API — use it first (fewer
integrations). The HTTP transport is injectable so this is testable without
network or keys; the default uses stdlib urllib.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

DATA_URL = "https://data.alpaca.markets"

Transport = Callable[[str, str, Dict[str, str]], object]  # (method, url, headers) -> json


@dataclass(frozen=True)
class Headline:
    symbols: List[str]
    headline: str
    summary: str
    created_at: str


def _urllib_get(method: str, url: str, headers: Dict[str, str]) -> object:
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode() or "{}")
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise RuntimeError(f"news fetch failed: {exc}") from exc


class AlpacaNewsClient:
    def __init__(
        self,
        keystore: Callable[[], Dict[str, str]],
        transport: Optional[Transport] = None,
    ) -> None:
        self._keystore = keystore
        self._transport = transport or _urllib_get

    def _headers(self) -> Dict[str, str]:
        creds = self._keystore()
        return {
            "APCA-API-KEY-ID": creds["api_key_id"],
            "APCA-API-SECRET-KEY": creds["api_secret_key"],
        }

    def recent(self, symbols: List[str], limit: int = 50) -> List[Headline]:
        sym = ",".join(symbols)
        url = f"{DATA_URL}/v1beta1/news?symbols={sym}&limit={limit}"
        raw = self._transport("GET", url, self._headers())
        rows = raw.get("news", []) if isinstance(raw, dict) else []
        return [
            Headline(
                symbols=n.get("symbols", []),
                headline=n.get("headline", ""),
                summary=n.get("summary", ""),
                created_at=n.get("created_at", ""),
            )
            for n in rows
        ]
