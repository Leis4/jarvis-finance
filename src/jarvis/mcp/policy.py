"""Exact, read-only capabilities for autonomous MCP tool execution."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from jarvis.mcp.errors import MCPPolicyDenied

_READ_TOOLS: dict[str, frozenset[str]] = {
    "moex": frozenset({
        "moex_ping", "iss_securities_search", "iss_reference", "iss_marketdata",
        "iss_candles", "iss_history", "iss_indices",
    }),
    "sber": frozenset({"sber_setup_status", "sber_products", "sber_operations",
                       "sber_operations_page", "sber_session_info"}),
    "tbank": frozenset({
        "get_portfolio", "get_quotes", "get_candles", "tbank_get_accounts",
        "tbank_get_portfolio", "tbank_get_positions", "tbank_get_cash_balances",
        "tbank_get_instruments", "tbank_get_quotes", "tbank_get_orders",
        "tbank_get_fills", "tbank_get_operations",
    }),
}


def require_read_capability(provider: str, tool_name: str, args: dict[str, Any] | None = None) -> None:
    """Reject unknown or write-capable tools before transport starts."""
    if (not isinstance(provider, str) or not isinstance(tool_name, str) or
            (args is not None and (not isinstance(args, dict) or
                                   any(not isinstance(key, str) for key in args)))):
        raise MCPPolicyDenied("MCP tool denied by capability policy")
    if tool_name not in _READ_TOOLS.get(provider, frozenset()):
        raise MCPPolicyDenied("MCP tool denied by capability policy")
    if provider == "sber" and tool_name == "sber_setup_status":
        arguments = args or {}
        if set(arguments) != {"profile"} or not isinstance(arguments["profile"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", arguments["profile"]):
            raise MCPPolicyDenied("Explicit Sber profile required")
    if provider == "sber" and tool_name in {"sber_products", "sber_operations", "sber_session_info", "sber_operations_page"}:
        arguments = args or {}
        sid = arguments.get("session_id")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
            raise MCPPolicyDenied("Explicit Sber session required")
        if tool_name == "sber_products":
            if set(arguments) - {"session_id", "force_update"} or type(arguments.get("force_update", False)) is not bool:
                raise MCPPolicyDenied("Invalid product arguments")
        elif tool_name == "sber_session_info":
            if set(arguments) - {"session_id", "check_live"} or arguments.get("check_live", False) is not False:
                raise MCPPolicyDenied("Live session probe denied")
        else:
            if set(arguments) - {"session_id", "resource", "offset", "limit", "from_date", "to_date"}:
                raise MCPPolicyDenied("Invalid pagination arguments")
            resource = arguments.get("resource")
            if not isinstance(resource, str) or not re.fullmatch(r"(card|ct-account|account):[A-Za-z0-9_-]{1,128}", resource):
                raise MCPPolicyDenied("Explicit resource required")
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 30)
            if type(offset) is not int or not 0 <= offset <= 1_000_000 or type(limit) is not int or not 1 <= limit <= 50:
                raise MCPPolicyDenied("Invalid pagination bounds")
            try:
                start = date.fromisoformat(arguments["from_date"])
                end = date.fromisoformat(arguments["to_date"])
                if start > end:
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise MCPPolicyDenied("Explicit ordered dates required") from None


def require_sber_read_runtime(command: list[str], env: dict[str, str]) -> None:
    """Reject write-enabled Sber startup before spawning a transport process."""
    if any(arg == "--allow-writes" or arg.startswith("--allow-writes=") for arg in command):
        raise MCPPolicyDenied("Sber write mode forbidden")
    if any(key.upper() == "SBER_MCP_ALLOW_WRITES" and value.strip().lower() not in {"", "0", "false", "off"} for key, value in env.items()):
        raise MCPPolicyDenied("Sber write mode forbidden")


def validate_sber_inventory(tools: Any) -> list[dict[str, Any]]:
    """Reject unknown/mutation advertisements and hide operator-only lifecycle tools."""
    lifecycle = {"sber_auth_start", "sber_auth_continue", "sber_auth_resend_otp", "sber_session_close"}
    if not isinstance(tools, list) or not 1 <= len(tools) <= 32:
        raise MCPPolicyDenied("Unexpected Sber tool inventory")
    names: set[str] = set()
    reads = []
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else None
        if type(name) is not str or name in names or name not in _READ_TOOLS["sber"] | lifecycle:
            raise MCPPolicyDenied("Unexpected Sber tool inventory")
        names.add(name)
        if name in _READ_TOOLS["sber"]:
            reads.append(tool)
    if not reads:
        raise MCPPolicyDenied("Unexpected Sber tool inventory")
    return reads
