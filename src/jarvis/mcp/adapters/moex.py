from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from jarvis.mcp.client import MCPClient
from jarvis.mcp.iss_parser import first_row, parse_iss_response
from jarvis.mcp.stdio_client import StdioMCPClient


class MoexMCP:
    """High-level wrapper for MOEX MCP server (iss_* tools)."""

    def __init__(
        self,
        client: MCPClient | StdioMCPClient,
        default_engine: str = "stock",
        default_market: str = "shares",
    ) -> None:
        self._c = client
        self.engine = default_engine
        self.market = default_market

    def ping(self) -> str:
        """Health check."""
        return str(self._c.call_tool("moex_ping", {}))

    def search_securities(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search securities by name or ticker. Filtered to traded stocks only."""
        result = self._c.call_tool("iss_securities_search", {
            "q": query,
            "engine": self.engine,
            "market": self.market,
            "is_trading": "1",
            "limit": limit,
        })
        return parse_iss_response(result, "securities")

    def get_all_shares(
        self,
        limit: int = 250,
        instrument: Literal["stock", "etf", "all"] = "stock",
    ) -> list[dict[str, Any]]:
        """Get list of traded shares on MOEX (filtered by instrument type)."""
        fetch_limit = limit * 4 if instrument != "all" else limit
        try:
            result = self._c.call_tool("iss_reference", {
                "operation": "securities_by_board",
                "engine": self.engine,
                "market": self.market,
                "board": "TQBR",
                "limit": fetch_limit,
            })
            shares = parse_iss_response(result, "securities")
            if not shares:
                shares = self.search_securities("", limit=fetch_limit)
        except Exception:  # noqa: BLE001
            shares = self.search_securities("", limit=fetch_limit)

        if instrument == "stock":
            filtered = [
                it
                for it in shares
                if it.get("INSTRID") == "EQIN" and str(it.get("SECTYPE")) in {"1", "2"}
            ]
        elif instrument == "etf":
            filtered = [
                it for it in shares if it.get("INSTRID") in {"IFTF", "IFA1"}
            ]
        else:
            filtered = shares

        return filtered[:limit]

    def get_all_bonds(
        self,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        """Get list of traded bonds on MOEX (OFZ and corporate)."""
        fetch_limit = limit * 4
        try:
            result = self._c.call_tool("iss_reference", {
                "operation": "securities_by_board",
                "engine": "stock",
                "market": "bonds",
                "board": "TQOB",
                "limit": fetch_limit,
            })
            bonds = parse_iss_response(result, "securities")
            if not bonds:
                bonds = parse_iss_response(
                    self._c.call_tool("iss_securities_search", {
                        "q": "",
                        "engine": "stock",
                        "market": "bonds",
                        "is_trading": "1",
                        "limit": fetch_limit,
                    }),
                    "securities",
                )
        except Exception:  # noqa: BLE001
            bonds = parse_iss_response(
                self._c.call_tool("iss_securities_search", {
                    "q": "",
                    "engine": "stock",
                    "market": "bonds",
                    "is_trading": "1",
                    "limit": fetch_limit,
                }),
                "securities",
            )

        filtered = [
            it
            for it in bonds
            if str(it.get("SECTYPE", "")).strip() in {"1", "2", "3", "4", "B", "C", "D"}
            or it.get("INSTRID") in {"EQOB", "BOF", "CB"}
            or "ОФЗ" in str(it.get("SHORTNAME", "")).upper()
            or "ОФЗ" in str(it.get("SECNAME", "")).upper()
        ]
        return (filtered or bonds)[:limit]


    def get_security_info(self, ticker: str) -> dict[str, Any]:
        """Get security card info."""
        result = self._c.call_tool("iss_reference", {
            "operation": "security",
            "engine": self.engine,
            "market": self.market,
            "secid": ticker,
        })
        return first_row(parse_iss_response(result, "securities"))

    def get_quote(self, ticker: str) -> dict[str, Any]:
        """Get current quote for one ticker (returns flat dict)."""
        result = self._c.call_tool("iss_marketdata", {
            "operation": "security_quotes",
            "engine": self.engine,
            "market": self.market,
            "secid": ticker,
        })
        return first_row(parse_iss_response(result, "marketdata"))

    def get_quotes(self, tickers: list[str]) -> list[dict[str, Any]]:
        """Get current quotes for multiple tickers."""
        out: list[dict[str, Any]] = []
        for t in tickers:
            q = self.get_quote(t)
            q["ticker"] = t
            out.append(q)
        return out

    def get_candles(
        self,
        ticker: str,
        interval: int = 24,
        days: int = 30,
        *, market: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get candles. interval in minutes (1, 10, 60, 24)."""
        till = datetime.now(UTC).date()
        frm = till - timedelta(days=days)
        result = self._c.call_tool("iss_candles", {
            "engine": self.engine,
            "market": market or self.market,
            "secid": ticker,
            "interval": interval,
            "from": frm.isoformat(),
            "till": till.isoformat(),
        })
        return parse_iss_response(result, "candles")

    def get_history(
        self,
        ticker: str,
        from_date: date | None = None,
        till_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Get daily trading history."""
        till = till_date or datetime.now(UTC).date()
        frm = from_date or (till - timedelta(days=30))
        result = self._c.call_tool("iss_history", {
            "operation": "daily",
            "engine": self.engine,
            "market": self.market,
            "secid": ticker,
            "from": frm.isoformat(),
            "till": till.isoformat(),
        })
        return parse_iss_response(result, "history")

    def list_indices(self) -> list[dict[str, Any]]:
        """List MOEX indices."""
        result = self._c.call_tool("iss_indices", {"operation": "list"})
        return parse_iss_response(result, "indices")

    def get_index(self, secid: str) -> dict[str, Any]:
        """Get one index by secid (e.g. IMOEX)."""
        result = self._c.call_tool("iss_indices", {
            "operation": "index",
            "secid": secid,
        })
        return first_row(parse_iss_response(result, "indices"))
