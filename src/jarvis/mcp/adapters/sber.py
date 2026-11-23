from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from jarvis.mcp.client import MCPClient
from jarvis.mcp.errors import MCPPolicyDenied
from jarvis.mcp.stdio_client import StdioMCPClient


def rub_account_subtotal(products: dict[str, Any]) -> Decimal:
    """Observe RUB accounts only; unknown coverage is never a bank-wide total."""
    accounts = products.get("accounts")
    if not isinstance(accounts, list) or not accounts or len(accounts) > 10000:
        raise ValueError("Sber account balances insufficient_data")
    seen: set[str] = set()
    total = Decimal(0)
    rub_count = 0
    for account in accounts:
        if not isinstance(account, dict):
            raise TypeError("Sber account balances insufficient_data")
        identity, balance = account.get("id"), account.get("balance")
        if not isinstance(identity, str) or not identity or identity in seen or not isinstance(balance, dict):
            raise ValueError("Sber account balances insufficient_data")
        seen.add(identity)
        currency, raw = balance.get("currency"), balance.get("amount")
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha() or not currency.isupper():
            raise ValueError("Sber account balances insufficient_data")
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)) or len(str(raw)) > 64:
            raise ValueError("Sber account balances insufficient_data")
        try:
            amount = Decimal(str(raw))
        except InvalidOperation:
            raise ValueError("Sber account balances insufficient_data") from None
        if not amount.is_finite() or amount.copy_abs() > Decimal("1e18"):
            raise ValueError("Sber account balances insufficient_data")
        if currency == "RUB":
            with localcontext() as context:
                context.prec = 32
                if amount != amount.quantize(Decimal("0.01")):
                    raise ValueError("Sber account balances insufficient_data")
                total += amount
            rub_count += 1
    if not rub_count:
        raise ValueError("Sber account balances insufficient_data")
    with localcontext() as context:
        context.prec = 32
        return total.quantize(Decimal("0.01"))


class SberAuthPending(RuntimeError):
    """Authentication needs a human action, not an automatic restart."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.code = str(response.get("code", "unknown"))
        self.next_action = str(response.get("next_action", "SUBMIT_SECRET"))
        self.state = str(response.get("state", "unknown"))
        self.session_id = str(response.get("session_id", ""))
        details = response.get("details", {})
        self.details = details if isinstance(details, dict) else {}
        super().__init__(f"Sber auth pending: {self.code}; next_action={self.next_action}")


class SberMCP:
    """High-level wrapper for sber-unofficial-mcp (read-only)."""

    def __init__(self, client: MCPClient | StdioMCPClient) -> None:
        self._c = client
        self._session_id: str | None = None
        self._pending_session_id: str | None = None
        self._expected_profile: str | None = None

    def bind_read_session(self, session_id: str, expected_profile: str) -> str:
        """Bind an operator-created session after a nonlive profile check."""
        from jarvis.mcp.policy import require_read_capability
        args = {"session_id": session_id, "check_live": False}
        require_read_capability("sber", "sber_session_info", args)
        result = self._c.call_tool("sber_session_info", args)
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise PermissionError("auth_required")
        data = result.get("data")
        if (not isinstance(data, dict) or data.get("state") != "authenticated" or
                data.get("session_id") != session_id):
            raise PermissionError("auth_required")
        if not expected_profile or data.get("profile") != expected_profile:
            raise ValueError("Sber profile mismatch")
        self._session_id = session_id
        self._expected_profile = expected_profile
        return session_id

    def read_session(self) -> str:
        """Revalidate the bound profile without starting authentication."""
        if self._session_id is None or self._expected_profile is None:
            raise PermissionError("auth_required")
        return self.bind_read_session(self._session_id, self._expected_profile)

    def open_session(self, mode: str = "auto") -> str:
        """General adapter never initiates operator authentication."""
        raise MCPPolicyDenied("Operator-only authentication required")

    def _handle_pin_challenge(self, challenge: dict[str, Any]) -> str:
        """Reject legacy secret delivery before reading environment or files."""
        raise MCPPolicyDenied("Operator-only authentication required")

    def continue_session(self, wait_seconds: int = 0) -> str:
        """Continue is restricted to the separate operator workflow."""
        raise MCPPolicyDenied("Operator-only authentication required")

    def close_session(self) -> None:
        """Unbind locally; close owned stdio without an auth-capable RPC."""
        self._session_id = self._pending_session_id = None
        self._expected_profile = None
        if isinstance(self._c, StdioMCPClient):
            self._c.close()

    def _require_session(self) -> str:
        if self._session_id is None:
            raise RuntimeError("session not opened")
        return self._session_id

    def get_products(self) -> dict[str, Any]:
        """Get accounts and cards with balances."""
        sid = self._require_session()
        result = self._c.call_tool("sber_products", {"session_id": sid})
        if not isinstance(result, dict) or result.get("status", "ok") != "ok":
            raise RuntimeError("Sber products unavailable")
        data = result.get("data", {})
        if not isinstance(data, dict) or not any(key in data for key in ("accounts", "cards")):
            raise RuntimeError("Invalid Sber products response")
        if any(not isinstance(data.get(key, []), list) for key in ("accounts", "cards")):
            raise RuntimeError("Invalid Sber product collections")
        return {
            "accounts": list(data.get("accounts", [])),
            "cards": list(data.get("cards", [])),
        }

    def get_accounts(self) -> list[dict[str, Any]]:
        """List accounts with balances."""
        return list(self.get_products().get("accounts", []))

    def get_cards(self) -> list[dict[str, Any]]:
        """List cards with balances."""
        return list(self.get_products().get("cards", []))

    def total_balance_rub(self) -> Decimal:
        """Compatibility subtotal of observed RUB accounts, not accepted coverage."""
        return rub_account_subtotal(self.get_products())

    def get_operations(
        self,
        resource: str,
        from_date: str,
        to_date: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get operations for one resource (card:ID or ct-account:ID)."""
        sid = self._require_session()
        result = self._c.call_tool("sber_operations", {
            "session_id": sid,
            "resource": resource,
            "from_date": from_date,
            "to_date": to_date,
            "limit": limit,
        })
        if not isinstance(result, dict) or result.get("status", "ok") != "ok":
            raise RuntimeError("Sber operations unavailable")
        data = result.get("data", {})
        if not isinstance(data, dict) or not isinstance(data.get("operations"), list):
            raise TypeError("Invalid Sber operations response")
        ops = list(data["operations"])
        if any(not isinstance(op, dict) for op in ops):
            raise RuntimeError("Invalid Sber operation entries")
        if len(ops) >= limit:
            raise RuntimeError("Sber operations may be truncated; shorten the import period")
        return ops

    def get_all_operations(
        self,
        from_date: str,
        to_date: str,
        limit_per_resource: int = 50,
    ) -> list[dict[str, Any]]:
        """Get operations from all accounts and cards."""
        out: list[dict[str, Any]] = []
        products = self.get_products()
        for acc in products.get("accounts", []):
            res = acc.get("operation_resource")
            if not res or not isinstance(res, str):
                continue
            operations = self.get_operations(res, from_date, to_date, limit_per_resource)
            out.extend({**op, "_source_resource": res, "_account_id": str(acc.get("id") or res)} for op in operations)
        for card in products.get("cards", []):
            res = card.get("operation_resource")
            if not res or not isinstance(res, str):
                continue
            operations = self.get_operations(res, from_date, to_date, limit_per_resource)
            out.extend({**op, "_source_resource": res, "_account_id": str(card.get("account_id") or card.get("id") or res)} for op in operations)
        return out
