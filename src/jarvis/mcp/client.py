from __future__ import annotations

from typing import Any

import httpx

from jarvis.mcp.errors import MCPToolNotFound, MCPUnavailable
from jarvis.mcp.policy import require_read_capability, validate_sber_inventory


class MCPClient:
    """Universal MCP client. Talks to MCP servers over HTTP/JSON-RPC."""

    def __init__(self, base_url: str, timeout_s: float = 30.0, name: str = "mcp") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.name = name

    def call_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        """Call MCP tool. Raises MCPUnavailable / MCPToolNotFound."""
        require_read_capability(self.name, tool_name, args)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/rpc",
                json=payload,
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as e:
            raise MCPUnavailable(f"{self.name}: {e}") from e
        if resp.status_code >= 500:
            raise MCPUnavailable(f"{self.name}: {resp.status_code}")
        if resp.status_code == 404:
            raise MCPToolNotFound(f"{self.name}: {tool_name}")
        if resp.status_code >= 400:
            raise MCPUnavailable(f"{self.name}: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if "error" in data:
            raise MCPUnavailable(f"{self.name}: {data['error']}")
        return data.get("result")

    def list_tools(self) -> list[dict[str, Any]]:
        """List available tools."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        try:
            resp = httpx.post(
                f"{self.base_url}/rpc",
                json=payload,
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as e:
            raise MCPUnavailable(f"{self.name}: {e}") from e
        if resp.status_code >= 400:
            raise MCPUnavailable(f"{self.name}: {resp.status_code}")
        res = resp.json().get("result", {})
        if isinstance(res, dict):
            tools = res.get("tools", [])
            if self.name == "sber":
                return validate_sber_inventory(tools)
            return list(tools) if isinstance(tools, list) else []
        return []
