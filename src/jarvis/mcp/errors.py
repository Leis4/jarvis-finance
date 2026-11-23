from __future__ import annotations


class MCPError(Exception):
    """Base exception for MCP errors."""


class MCPUnavailable(MCPError):
    """MCP server or transport is unavailable."""


class MCPToolNotFound(MCPError):
    """MCP tool was not found on server."""


class MCPPolicyDenied(MCPError):
    """An autonomous MCP call lacks an explicitly allowed read capability."""
