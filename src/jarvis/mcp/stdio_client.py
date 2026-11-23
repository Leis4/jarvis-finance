from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, cast

from jarvis.mcp.errors import MCPToolNotFound, MCPUnavailable
from jarvis.mcp.policy import (
    require_read_capability,
    require_sber_read_runtime,
    validate_sber_inventory,
)

_RUNTIME_ENV_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "TMPDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "LANG",
    "VIRTUAL_ENV",
})


def minimal_subprocess_env() -> dict[str, str]:
    """Keep runtime paths while excluding unrelated credentials."""
    return {key: value for key, value in os.environ.items() if key.upper() in _RUNTIME_ENV_KEYS}


class StdioMCPClient:
    """MCP client over stdio. Wraps a subprocess speaking JSON-RPC."""

    def __init__(
        self,
        command: list[str],
        name: str = "mcp",
        timeout_s: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.name = name
        self.timeout_s = timeout_s
        self._env = dict(env) if env is not None else minimal_subprocess_env()
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._initialized = False
        self._lines: queue.Queue[bytes] | None = None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        if self.name == "sber":
            require_sber_read_runtime(self.command, self._env)
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self.close()
        resolved = self._resolve_command(self.command)
        try:
            self._proc = subprocess.Popen(
                resolved,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._env,
                bufsize=0,
            )
        except OSError as e:
            raise MCPUnavailable(f"{self.name}: failed to start: {e}") from e
        if not self._initialized:
            self._initialize()
        return self._proc

    def _resolve_command(self, cmd: list[str]) -> list[str]:
        """Resolve first element to full path, tries .exe/.cmd/.bat on Windows."""
        if not cmd:
            return cmd
        exe = cmd[0]
        resolved = shutil.which(exe)
        if resolved is None and not exe.endswith((".exe", ".cmd", ".bat")):
            for ext in (".exe", ".cmd", ".bat"):
                resolved = shutil.which(exe + ext)
                if resolved:
                    break
        if resolved:
            return [resolved, *cmd[1:]]
        return cmd

    def _write(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPUnavailable(f"{self.name}: stdin not available")
        data = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self.close()
            raise MCPUnavailable(f"{self.name}: write failed: {e}") from e

    def _read_response(self, expected_id: int) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise MCPUnavailable(f"{self.name}: stdout not available")
        if self._lines is None:
            lines: queue.Queue[bytes] = queue.Queue()
            stdout = self._proc.stdout

            def read_stdout() -> None:
                try:
                    while True:
                        line = stdout.readline()
                        lines.put(line)
                        if not line:
                            return
                except (OSError, ValueError):
                    lines.put(b"")

            self._lines = lines
            threading.Thread(target=read_stdout, daemon=True).start()
        responses = self._lines
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            try:
                line = responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if not line:
                self.close()
                raise MCPUnavailable(f"{self.name}: process closed stdout")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == expected_id:
                return cast(dict[str, Any], msg)
        self.close()
        raise MCPUnavailable(f"{self.name}: read timeout")

    def _initialize(self) -> None:
        init_id = self._next_id()
        self._write({
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "jarvis", "version": "0.1.0"},
            },
        })
        self._read_response(init_id)
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    def call_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        """Call MCP tool over stdio. Raises MCPUnavailable / MCPToolNotFound."""
        require_read_capability(self.name, tool_name, args)
        with self._lock:
            self._ensure_started()
            req_id = self._next_id()
            self._write({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            })
            msg = self._read_response(req_id)
            if "error" in msg:
                err = msg["error"]
                code = err.get("code") if isinstance(err, dict) else None
                if code == -32601:
                    raise MCPToolNotFound(f"{self.name}: {tool_name}")
                raise MCPUnavailable(f"{self.name}: {err}")
            result = msg.get("result", {})
            if isinstance(result, dict) and "content" in result:
                content = result["content"]
                if isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict) and "text" in first:
                        text = first["text"]
                        try:
                            return json.loads(text)
                        except (json.JSONDecodeError, TypeError):
                            return text
            return result

    def list_tools(self) -> list[dict[str, Any]]:
        """List available tools."""
        with self._lock:
            self._ensure_started()
            req_id = self._next_id()
            self._write({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/list",
            })
            msg = self._read_response(req_id)
            if "error" in msg:
                raise MCPUnavailable(f"{self.name}: {msg['error']}")
            result = msg.get("result", {})
            if isinstance(result, dict):
                tools = result.get("tools", [])
                if self.name == "sber":
                    return validate_sber_inventory(tools)
                return list(tools) if isinstance(tools, list) else []
            return []

    def close(self) -> None:
        """Stop the subprocess."""
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._proc.kill()
                except OSError:
                    pass
            self._proc = None
            self._initialized = False
        self._lines = None
