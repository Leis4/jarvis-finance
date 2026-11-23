from __future__ import annotations

from typing import Any

from jarvis.invest.core.interfaces import BrokerAdapter, OrderRequest, OrderResult
from jarvis.mcp.errors import MCPPolicyDenied


class TBankMCPAdapter(BrokerAdapter):
    """T-Invest adapter via MCP server."""

    def __init__(self, mcp_client: Any | None = None) -> None:
        self._mcp = mcp_client

    def _require_mcp(self) -> Any:
        if self._mcp is None:
            raise NotImplementedError("MCP client not configured")
        return self._mcp

    def get_accounts(self) -> list[dict[str, Any]]:
        mcp = self._require_mcp()
        res = mcp.call_tool("tbank_get_accounts", {})
        return list(res) if isinstance(res, list) else []

    def get_portfolio(self, account_id: str | None = None) -> dict[str, Any]:
        mcp = self._require_mcp()
        res = mcp.call_tool("tbank_get_portfolio", {"account_id": account_id} if account_id else {})
        return dict(res) if isinstance(res, dict) else {}

    def get_positions(self, account_id: str | None = None) -> list[dict[str, Any]]:
        mcp = self._require_mcp()
        res = mcp.call_tool("tbank_get_positions", {"account_id": account_id} if account_id else {})
        return list(res) if isinstance(res, list) else []

    def get_cash_balances(self, account_id: str | None = None) -> list[dict[str, Any]]:
        mcp = self._require_mcp()
        res = mcp.call_tool("tbank_get_cash_balances", {"account_id": account_id} if account_id else {})
        return list(res) if isinstance(res, list) else []

    def get_instruments(self) -> list[dict[str, Any]]:
        mcp = self._require_mcp()
        res = mcp.call_tool("tbank_get_instruments", {})
        return list(res) if isinstance(res, list) else []

    def get_quotes(self, instrument_ids: list[str]) -> list[dict[str, Any]]:
        mcp = self._require_mcp()
        res = mcp.call_tool("tbank_get_quotes", {"symbols": instrument_ids})
        return list(res) if isinstance(res, list) else []

    def place_order(self, order: OrderRequest) -> OrderResult:
        raise MCPPolicyDenied("Live broker orders are disabled")

    def cancel_order(self, order_id: str, account_id: str | None = None) -> dict[str, Any]:
        raise MCPPolicyDenied("Live broker order changes are disabled")

    def get_orders(self, account_id: str | None = None) -> list[dict[str, Any]]:
        mcp = self._require_mcp()
        res = mcp.call_tool("tbank_get_orders", {"account_id": account_id} if account_id else {})
        return list(res) if isinstance(res, list) else []

    def get_fills(self, account_id: str | None = None) -> list[dict[str, Any]]:
        mcp = self._require_mcp()
        res = mcp.call_tool("tbank_get_fills", {"account_id": account_id} if account_id else {})
        return list(res) if isinstance(res, list) else []

    def get_operations(self, account_id: str | None = None) -> list[dict[str, Any]]:
        mcp = self._require_mcp()
        res = mcp.call_tool("tbank_get_operations", {"account_id": account_id} if account_id else {})
        return list(res) if isinstance(res, list) else []

    def validate_order(self, order: OrderRequest) -> dict[str, Any]:
        reasons: list[str] = []
        if order.quantity <= 0:
            reasons.append("quantity_must_be_positive")
        return {"valid": not reasons, "reasons": reasons}

    def healthcheck(self) -> dict[str, Any]:
        if self._mcp is None:
            return {"status": "degraded", "mode": "tbank_mcp", "reason": "mcp_client_not_configured"}
        return {"status": "ok", "mode": "tbank_mcp"}
