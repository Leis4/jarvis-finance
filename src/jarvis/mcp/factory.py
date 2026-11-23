from __future__ import annotations

import os
import shlex
from pathlib import Path

from jarvis.core.config import settings
from jarvis.mcp.client import MCPClient
from jarvis.mcp.stdio_client import StdioMCPClient, minimal_subprocess_env


def _parse_command(cmd: str) -> list[str]:
    return [part.strip('"') for part in shlex.split(cmd, posix=False)]


def get_tbank_mcp() -> StdioMCPClient | MCPClient | None:
    if settings.tbank_mcp_command:
        env = minimal_subprocess_env()
        if token := os.environ.get("TBANK_INVEST_TOKEN"):
            env["TBANK_INVEST_TOKEN"] = token
        return StdioMCPClient(
            _parse_command(settings.tbank_mcp_command),
            name="tbank",
            env=env,
        )
    if settings.tbank_mcp_url:
        return MCPClient(settings.tbank_mcp_url, name="tbank")
    return None


def get_sber_mcp() -> StdioMCPClient | MCPClient | None:
    if settings.sber_mcp_command:
        home = os.environ.get("SBER_MCP_HOME", "").strip()
        if not home or not Path(home).is_absolute():
            raise ValueError("Explicit absolute SBER_MCP_HOME required")
        env = minimal_subprocess_env()
        env["SBER_MCP_HOME"] = home
        return StdioMCPClient(
            _parse_command(settings.sber_mcp_command),
            name="sber",
            env=env,
        )
    if settings.sber_mcp_url:
        return MCPClient(settings.sber_mcp_url, name="sber")
    return None


def get_moex_mcp() -> StdioMCPClient | MCPClient | None:
    if settings.moex_mcp_command:
        return StdioMCPClient(
            _parse_command(settings.moex_mcp_command),
            name="moex",
        )
    if settings.moex_mcp_url:
        return MCPClient(settings.moex_mcp_url, name="moex")
    return None
