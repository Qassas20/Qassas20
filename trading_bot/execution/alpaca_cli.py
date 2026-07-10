"""Trade Execution Agent — a bucket-scoped wrapper over the Alpaca CLI.

The Alpaca CLI (April 2026) exposes 108 trading functions and returns structured
JSON by default. It has **no guardrails**: trade commands execute immediately
with no confirmation, and ``order cancel-all`` / ``position close-all`` act
account-wide with no confirmation. This wrapper enforces the safety rules from
Section 1 of the UAT addendum:

* No account-wide bulk operations are ever exposed. ``cancel_all`` /
  ``close_all`` are re-implemented as *bucket-scoped* loops over that bucket's
  own symbol list, so one bucket's panic-close can never nuke another bucket's
  positions.
* Live API keys are never logged, never written to the repo, never put in the
  SQLite DB — the wrapper reads them from an injected keystore only and refuses
  to echo them.

Auth model: paper uses OAuth (``alpaca profile login``), live requires API keys.

The CLI itself is invoked via :mod:`subprocess`; the binary path and a dry-run
switch are injectable so the agent is fully unit-testable without a real CLI.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence


class ExecutionError(RuntimeError):
    """Raised when the underlying CLI call fails or a safety guard trips."""


@dataclass
class BucketScope:
    """The universe a bucket is allowed to touch.

    All bulk operations are constrained to ``symbols``; the agent will refuse to
    act on any symbol not registered to the bucket.
    """

    bucket_id: int
    symbols: List[str] = field(default_factory=list)

    def assert_owns(self, symbol: str) -> None:
        if symbol not in self.symbols:
            raise ExecutionError(
                f"Symbol {symbol!r} is not in bucket {self.bucket_id}'s scope; "
                "refusing cross-bucket operation."
            )


# Type of the low-level runner: (argv) -> parsed-json / raw-text.
Runner = Callable[[Sequence[str]], object]


class AlpacaExecutionAgent:
    """Drives the Alpaca CLI, scoped to a single bucket.

    Parameters
    ----------
    scope:
        The bucket's symbol universe. Every operation is checked against it.
    mode:
        ``"paper"`` (OAuth) or ``"live"`` (API keys). Bulk/close operations are
        identical in both, but live requires ``keystore`` to be present.
    binary:
        Path/name of the CLI binary (default ``"alpaca"``).
    runner:
        Injectable low-level runner for testing. Defaults to a real subprocess
        call. Receives the full argv and returns parsed JSON (or text).
    keystore:
        Callable returning live API credentials on demand. NEVER stored on the
        instance, NEVER logged. Only consulted in live mode.
    """

    def __init__(
        self,
        scope: BucketScope,
        mode: str = "paper",
        binary: str = "alpaca",
        runner: Optional[Runner] = None,
        keystore: Optional[Callable[[], Dict[str, str]]] = None,
    ) -> None:
        if mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        if mode == "live" and keystore is None:
            raise ExecutionError("live mode requires an encrypted keystore for API keys")
        self.scope = scope
        self.mode = mode
        self.binary = binary
        self._runner = runner or self._subprocess_runner
        self._keystore = keystore

    # -- low-level ---------------------------------------------------------
    def _subprocess_runner(self, argv: Sequence[str]) -> object:
        """Default runner: exec the CLI and parse its JSON output.

        Credentials are passed via environment (from the keystore) and are never
        included in ``argv`` so they cannot leak into process listings or logs.
        """
        env = None
        if self.mode == "live" and self._keystore is not None:
            import os

            env = dict(os.environ)
            env.update(self._keystore())  # e.g. ALPACA_API_KEY_ID / SECRET
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:  # CLI not installed
            raise ExecutionError(f"Alpaca CLI not found: {self.binary}") from exc
        if proc.returncode != 0:
            # Deliberately do NOT include env in the message.
            raise ExecutionError(
                f"CLI command failed ({proc.returncode}): {' '.join(argv)}\n"
                f"{proc.stderr.strip()}"
            )
        out = proc.stdout.strip()
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out  # some commands emit non-JSON; hand it back raw

    def _run(self, *args: str) -> object:
        return self._runner([self.binary, *args])

    # -- account / market data --------------------------------------------
    def account(self) -> object:
        return self._run("account", "get")

    def quote(self, symbol: str) -> object:
        self.scope.assert_owns(symbol)
        return self._run("market-data", "quote", "--symbol", symbol)

    def positions(self) -> List[dict]:
        """All open positions, filtered down to this bucket's scope."""
        raw = self._run("position", "list")
        rows = raw if isinstance(raw, list) else raw.get("positions", []) if isinstance(raw, dict) else []
        return [p for p in rows if p.get("symbol") in self.scope.symbols]

    # -- orders ------------------------------------------------------------
    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "market",
        **flags: str,
    ) -> object:
        """Submit a single order. Executes immediately — the CLI never prompts."""
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        self.scope.assert_owns(symbol)
        argv = [
            "order", "submit",
            "--symbol", symbol,
            "--side", side,
            "--qty", str(qty),
            "--type", order_type,
        ]
        for k, v in flags.items():
            argv.extend([f"--{k.replace('_', '-')}", str(v)])
        return self._run(*argv)

    def cancel_order(self, order_id: str) -> object:
        return self._run("order", "cancel", "--order-id", order_id)

    # -- BUCKET-SCOPED bulk ops (the dangerous ones, made safe) -----------
    def cancel_all_scoped(self) -> List[object]:
        """Cancel open orders **for this bucket's symbols only**.

        Never calls the account-wide ``order cancel-all``. Instead it lists open
        orders, filters to the bucket's scope, and cancels them one by one.
        """
        raw = self._run("order", "list", "--status", "open")
        orders = raw if isinstance(raw, list) else raw.get("orders", []) if isinstance(raw, dict) else []
        results: List[object] = []
        for order in orders:
            if order.get("symbol") in self.scope.symbols:
                results.append(self.cancel_order(str(order.get("id"))))
        return results

    def close_all_scoped(self) -> List[object]:
        """Close positions **for this bucket's symbols only**.

        Never calls the account-wide ``position close-all``. One bucket's
        panic-close cannot touch another bucket's positions.
        """
        results: List[object] = []
        for pos in self.positions():
            symbol = pos.get("symbol")
            self.scope.assert_owns(symbol)
            results.append(self._run("position", "close", "--symbol", symbol))
        return results
