from __future__ import annotations

from typing import Any

from jarvis.mcp.client import MCPClient


class TBankMCP:
    """High-level wrapper for t-bank-invest-mcp-read-only."""

    def __init__(self, client: MCPClient) -> None:
        self._c = client

    def get_portfolio(self) -> list[dict[str, Any]]:
        res = self._c.call_tool("get_portfolio", {})
        return list(res) if isinstance(res, list) else []

    def get_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        res = self._c.call_tool("get_quotes", {"tickers": symbols})
        return list(res) if isinstance(res, list) else []

    def get_candles(self, symbol: str, interval: str = "day", days: int = 30) -> list[dict[str, Any]]:
        res = self._c.call_tool("get_candles", {"ticker": symbol, "interval": interval, "days": days})
        return list(res) if isinstance(res, list) else []
