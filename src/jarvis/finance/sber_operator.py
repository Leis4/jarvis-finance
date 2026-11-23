"""Explicit operator lifecycle rehearsal; live installation remains unaccepted."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from jarvis.mcp.errors import MCPError, MCPUnavailable
from jarvis.mcp.policy import require_read_capability
from jarvis.mcp.stdio_client import StdioMCPClient


def connection_status() -> dict[str, Any]:
    """Observe a nonsecret codec probe; never inspect profiles or start SDK/auth."""
    from jarvis.finance.windows_session_store import (
        SessionStorageUnavailable,
        protect_state,
        unprotect_state,
    )
    try:
        probe = b"jarvis-nonsecret-status-probe"
        storage = "available" if unprotect_state(protect_state(probe)) == probe else "unavailable"
    except SessionStorageUnavailable:
        storage = "unavailable"
    return {"status": "blocked", "storage_status": storage, "binding_scope": "disposable_only",
            "live_allowed": False, "apply_allowed": False, "database_modified": False,
            "bank_access_verified": False, "general_auth_allowed": False, "general_mutations_allowed": False,
            "balance": None, "coverage": "unknown", "last_success": None,
            "blockers": ["Launcher и межпроцессный challenge проверены только на synthetic fixtures; реальный runtime не принят.",
                         "Настоящий login/read требует отдельного явного операторского согласия.",
                         "Банковское покрытие и денежная семантика не подтверждены."]}


class OperatorBlocked(Exception):
    """Carry only a fixed nonprivate error code."""


class WorkerReadScope:
    """One consumed worker lifecycle, checked again at each SDK transport boundary."""

    def __init__(self, permit: Path, *, synthetic: bool) -> None:
        import os
        from contextvars import ContextVar

        from jarvis.finance.sber_windows_compat import operator_authorization
        self.permit, self.synthetic = permit, synthetic
        self.initial = operator_authorization(permit, synthetic=synthetic)
        self.session: str | None = None
        self.resource: str | None = None
        self.resources: set[str] = set()
        self.products_result: dict[str, Any] | None = None
        self.started = self.page_reserved = self.page_sent = False
        self.http_calls = self.product_calls = 0
        self.action: ContextVar[str | None] = ContextVar("operator_read_action", default=None)
        try:
            fd = os.open(permit.with_suffix(".worker"), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except OSError:
            raise OperatorBlocked("operator_authorization_replayed") from None
        os.close(fd)

    def check(self) -> dict[str, Any]:
        from jarvis.finance.sber_windows_compat import operator_authorization
        body = operator_authorization(self.permit, synthetic=self.synthetic)
        mutable = {"resource", "selected", "closed"}
        if any(body[key] != value for key, value in self.initial.items() if key not in mutable):
            raise OperatorBlocked("operator_authorization_changed")
        if body["selected"]:
            if body["resource"] not in self.resources or self.resource not in {None, body["resource"]}:
                raise OperatorBlocked("operator_resource_changed")
            self.resource = body["resource"]
        elif self.resource is not None:
            raise OperatorBlocked("operator_resource_changed")
        return body

    def bind_session(self, profile: str, session: str) -> None:
        self.check()
        if profile != self.initial["profile"] or self.session is not None:
            raise OperatorBlocked("operator_session_changed")
        self.session = session

    def install(self, server: Any, transport_factory: Any) -> None:
        """Guard original SDK methods and only five registered MCP lifecycle/read tools."""
        import functools
        import importlib
        import inspect
        from urllib.parse import parse_qs, urlsplit

        original_new = server._REG._new_state
        def new_state(profile: str, mode: str, state: str) -> Any:
            result = original_new(profile, mode, state)
            self.bind_session(profile, result.session_id)
            return result
        server._REG._new_state = new_state
        async def start_pin(reg: Any, cfg: Any, profile: str) -> Any:
            # Public MCP's pin mode expects an exported browser-cookie file.
            # Our enrolled SDK profile already holds those cookies, encrypted.
            self.check()
            path = server._profile_path(reg, profile)
            if not path.exists():
                raise OperatorBlocked("operator_saved_profile_required")
            state = reg._new_state(profile, "pin", "awaiting_pin")
            state.auth = server.AsyncPinAuth(server.SessionBundle.load(path))
            return server._arm(reg, state, "pin_required", ["pin"])
        server._start_pin = start_pin

        allowed = {"sber_auth_start", "sber_auth_continue", "sber_session_info", "sber_products", "sber_operations_page"}
        def wrap(fn: Any) -> Any:
            signature = inspect.signature(fn)
            @functools.wraps(fn)
            async def guarded(*args: Any, **kwargs: Any) -> Any:
                body = self.check()
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                values, name = bound.arguments, fn.__name__
                if name == "sber_auth_start":
                    if self.started or values["profile"] != body["profile"] or values["mode"] != body["auth_mode"]:
                        raise OperatorBlocked("operator_auth_denied")
                    self.started = True
                elif values.get("session_id") != self.session or self.session is None:
                    raise OperatorBlocked("operator_session_changed")
                if name == "sber_auth_continue" and (values.get("wait_seconds") != 0 or values.get("input_lines") is not None):
                    raise OperatorBlocked("operator_secret_rpc_denied")
                if name == "sber_auth_continue" and values.get("use_audio_captcha") is not False:
                    raise OperatorBlocked("operator_secret_rpc_denied")
                if name == "sber_products" and values.get("force_update") is not False:
                    raise OperatorBlocked("operator_write_flag_denied")
                if name == "sber_session_info" and values.get("check_live") is not False:
                    raise OperatorBlocked("operator_live_probe_denied")
                if name == "sber_operations_page":
                    expected = {"session_id": self.session, "resource": body["resource"], "offset": 0,
                        "limit": 5, "from_date": body["from_date"], "to_date": body["to_date"]}
                    if not body["selected"] or dict(values) != expected or self.page_reserved:
                        raise OperatorBlocked("operator_page_denied")
                    self.page_reserved = True
                token = self.action.set(name)
                try:
                    if name == "sber_products" and self.products_result is not None:
                        import copy
                        result = copy.deepcopy(self.products_result)
                    else:
                        result = await fn(*args, **kwargs)
                    if name == "sber_auth_start" and values["mode"] == "auto" and result.get("code") == "pin_required":
                        raise OperatorBlocked("manual_auth_required")
                    if name == "sber_products" and result.get("status") == "ok":
                        self.products_result = result
                        data = result["data"]
                        self.resources = {p["operation_resource"] for key in ("accounts", "cards") for p in data[key]
                                          if isinstance(p.get("operation_resource"), str)}
                    return result
                except OperatorBlocked as error:
                    reason = str(error) if re.fullmatch(r"[a-z_]+", str(error)) else "operator_denied"
                    return server.err("operator_scope_denied", server.NA_ABORT, details={"reason": reason})
                finally:
                    self.action.reset(token)
            return guarded
        server._TOOLS = [wrap(fn) for fn in server._TOOLS if fn.__name__ in allowed]

        scope = self
        class GuardedTransport:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                scope.check()
                if scope.action.get() not in allowed:
                    raise OperatorBlocked("operator_transport_without_consent")
                self.transport = transport_factory(*args, **kwargs)

            async def request(self, method: str, url: str, kwargs: dict[str, Any]) -> Any:
                body = scope.check()
                active = scope.action.get()
                parsed = urlsplit(url)
                paths = {"GET": {"/CSAFront/index.do", "/CSAFront/captcha.png", "/app/main", "/main"},
                    "FORM": {"/CSAFront/authMainJson.do"}, "POST": {"/CSAFront/api/v1/pin/create", "/CSAFront/api/v1/pin/begin", "/CSAFront/api/v1/pin/logon", "/CSAFront/api/v1/pin/otp/confirm",
                    "/CSAFront/api/v1/auth", "/api/warmUpSession", "/main-screen/rest/v2/m1/web/section/meta", "/uoh-bh/v1/operations/list"}}
                query = parse_qs(parsed.query, strict_parsing=True)
                captcha = parsed.path == "/CSAFront/captcha.png" and set(query) == {"noc"} and len(query["noc"]) == 1 and query["noc"][0].isascii() and query["noc"][0].isdigit()
                if (active not in allowed or parsed.scheme != "https" or parsed.netloc != "online.sberbank.ru"
                        or parsed.fragment or (parsed.query and not captcha) or parsed.path not in paths.get(method, set())
                        or scope.http_calls >= 100):
                    raise OperatorBlocked("operator_endpoint_denied")
                auth = active in {"sber_auth_start", "sber_auth_continue"}
                if (parsed.path.startswith("/CSAFront/") or parsed.path in {"/app/main", "/main"}) and not auth:
                    raise OperatorBlocked("operator_reauthentication_denied")
                if method == "FORM" and kwargs["data"].get("operation") not in {"button.begin", "button.next"}:
                    raise OperatorBlocked("operator_auth_transition_denied")
                if parsed.path == "/main-screen/rest/v2/m1/web/section/meta":
                    if active != "sber_products" or scope.product_calls or kwargs.get("json") != {"withData": True, "forceUpdate": False}:
                        raise OperatorBlocked("operator_products_denied")
                    scope.product_calls += 1
                if parsed.path == "/uoh-bh/v1/operations/list":
                    http: Any = importlib.import_module("sber_unofficial._http")
                    _request_datetime = http._request_datetime
                    expected = {"paginationOffset": 0, "paginationSize": 6, "showHidden": False,
                        "showNotTransactionBonuses": True, "showOpenBanking": True,
                        "from": _request_datetime(date.fromisoformat(body["from_date"])),
                        "to": _request_datetime(date.fromisoformat(body["to_date"]), end=True), "usedResource": [scope.resource]}
                    if active != "sber_operations_page" or not scope.page_reserved or scope.page_sent or kwargs.get("json") != expected:
                        raise OperatorBlocked("operator_operation_transport_denied")
                    scope.page_sent = True
                    kwargs = {**kwargs, "json": {**expected, "paginationSize": 5}}
                scope.http_calls += 1
                response = await getattr(self.transport, {"GET": "get", "POST": "post", "FORM": "post_form"}[method])(url, **kwargs)
                if parsed.path == "/uoh-bh/v1/operations/list":
                    try:
                        rows = response.json()["body"]["operations"]
                        if not isinstance(rows, list) or len(rows) > 5:
                            raise ValueError
                        if any(not isinstance(r, dict) or scope.resource not in {
                                r.get("fromResource", {}).get("id"), r.get("toResource", {}).get("id")} for r in rows):
                            raise ValueError
                    except (ValueError, TypeError, KeyError):
                        raise OperatorBlocked("operator_operations_response_denied") from None
                return response

            async def get(self, url: str, **kwargs: Any) -> Any:
                return await self.request("GET", url, kwargs)

            async def post(self, url: str, **kwargs: Any) -> Any:
                return await self.request("POST", url, kwargs)

            async def post_form(self, url: str, **kwargs: Any) -> Any:
                return await self.request("FORM", url, kwargs)

            def cookie_jar(self) -> Any:
                return self.transport.cookie_jar()

            async def aclose(self) -> None:
                await self.transport.aclose()
        for module in ("sber_unofficial.pin", "sber_unofficial.client"):
            target: Any = importlib.import_module(module)
            target.CurlAsyncTransport = GuardedTransport

    def enforce_raw_tools(self, native: Any) -> None:
        """Reject raw extra fields before MCP's permissive input-model conversion."""
        manager = native._tool_manager
        original = manager.call_tool
        fields = {"sber_auth_start": {"profile", "mode"}, "sber_auth_continue": {"session_id", "wait_seconds", "use_audio_captcha"},
                  "sber_session_info": {"session_id", "check_live"}, "sber_products": {"session_id", "force_update"},
                  "sber_operations_page": {"session_id", "resource", "offset", "limit", "from_date", "to_date"}}
        async def strict(name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            self.check()
            if name not in fields or not isinstance(arguments, dict) or set(arguments) - fields[name]:
                raise OperatorBlocked("operator_raw_tool_denied")
            if name == "sber_operations_page" and (type(arguments.get("limit")) is not int or arguments["limit"] != 5
                    or type(arguments.get("offset")) is not int or arguments["offset"] != 0):
                raise OperatorBlocked("operator_raw_page_denied")
            if name == "sber_auth_continue" and (type(arguments.get("wait_seconds")) is not int or arguments["wait_seconds"] != 0):
                raise OperatorBlocked("operator_raw_auth_denied")
            for flag in ("force_update", "check_live", "use_audio_captcha"):
                if flag in arguments and arguments[flag] is not False:
                    raise OperatorBlocked("operator_raw_flag_denied")
            return await original(name, arguments, *args, **kwargs)
        manager.call_tool = strict


class OperatorClient(Protocol):
    def call_tool(self, name: str, args: dict[str, Any]) -> Any: ...
    def operator_auth(self, name: str, args: dict[str, Any]) -> Any: ...
    def close(self) -> None: ...


class OperatorStdio(StdioMCPClient):
    """Dedicated consent-bound process, never used by factory/scheduler/LLM."""

    def __init__(self, command: list[str], *, consent: bool, env: dict[str, str]) -> None:
        if consent is not True:
            raise OperatorBlocked("operator_consent_required")
        self._started_once = False
        self._terminal = False
        self._demo_read_permit: tuple[Path, str] | None = None
        super().__init__(command, name="sber", timeout_s=120,
            env=env)

    def bind_demo_read_authorization(self, path: Path, session_id: str) -> None:
        """Bind a synthetic one-shot receipt; cannot enable genuine transport."""
        from jarvis.finance.sber_windows_compat import consume_demo_read_authorization
        if self._demo_read_permit is not None:
            raise OperatorBlocked("read_authorization_already_bound")
        consume_demo_read_authorization(path, session_id)
        self._demo_read_permit = (path, session_id)

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if self._demo_read_permit is not None:
            from jarvis.finance.sber_windows_compat import reserve_demo_read
            path, session = self._demo_read_permit
            reserve_demo_read(path, session_id=session, tool=name, arguments=args)
        return super().call_tool(name, args)

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        if self._terminal or (self._started_once and (self._proc is None or self._proc.poll() is not None)):
            raise OperatorBlocked("process_ended_auth_required")
        process = super()._ensure_started()
        # Base startup closes empty stale pipes; that is not an owner cancellation.
        self._terminal = False
        self._started_once = True
        return process

    def operator_auth(self, name: str, args: dict[str, Any]) -> Any:
        """Allow only fixed operator lifecycle calls, never arbitrary mutations."""
        if name == "sber_auth_start":
            if set(args) != {"profile", "mode"} or args["mode"] not in {"auto", "pin", "cold"}:
                raise OperatorBlocked("invalid_auth_arguments")
            if not isinstance(args["profile"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args["profile"]):
                raise OperatorBlocked("invalid_profile")
        elif name == "sber_auth_continue":
            if set(args) != {"session_id", "wait_seconds"} or type(args["wait_seconds"]) is not int or args["wait_seconds"] != 0:
                raise OperatorBlocked("invalid_auth_arguments")
            require_read_capability("sber", "sber_session_info", {"session_id": args["session_id"]})
        else:
            raise OperatorBlocked("operator_tool_denied")
        with self._lock:
            self._ensure_started()
            ident = self._next_id()
            self._write({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
            response = self._read_response(ident)
            if "error" in response:
                raise OperatorBlocked("operator_rpc_failed")
            result = response.get("result")
            if not isinstance(result, dict):
                raise OperatorBlocked("operator_response_invalid")
            try:
                return json.loads(result["content"][0]["text"])
            except (KeyError, IndexError, TypeError, ValueError):
                raise OperatorBlocked("operator_response_invalid") from None

    def close(self) -> None:
        self._terminal = True
        if self._demo_read_permit is not None:
            from jarvis.finance.sber_windows_compat import (
                revoke_demo_read_authorization,
            )
            try:
                revoke_demo_read_authorization(self._demo_read_permit[0])
            except (ValueError, OSError):
                pass  # Expired/revoked permits already refuse; still close the process.
        process = self._proc
        if process is not None:
            self._terminal = True
        super().close()
        if process is not None:
            try:
                process.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                raise OperatorBlocked("process_cleanup_failed") from None
            finally:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()


def _data(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") != "ok" or not isinstance(value.get("data"), dict):
        raise OperatorBlocked("session_or_source_unavailable")
    return dict(value["data"])


def smoke(client: OperatorClient, *, profile: str, resource: str, start: date,
          end: date, limit: int, consent: bool, challenge: Any) -> dict[str, Any]:
    """Rehearse bounded operator auth and one page; return no private fields."""
    try:
        if consent is not True:
            raise OperatorBlocked("operator_consent_required")
        if start > end or (end - start).days > 7 or type(limit) is not int or not 1 <= limit <= 5:
            raise OperatorBlocked("invalid_smoke_scope")
        require_read_capability("sber", "sber_operations_page", {"session_id": "preflight", "resource": resource,
            "offset": 0, "limit": limit, "from_date": start.isoformat(), "to_date": end.isoformat()})
        setup = _data(client.call_tool("sber_setup_status", {"profile": profile}))
        if setup.get("writes_allowed") is not False:
            raise OperatorBlocked("writes_must_be_disabled")
        auth = client.operator_auth("sber_auth_start", {"profile": profile, "mode": "auto"})
        # Human handles the server's file-delivery challenge. No secret is an RPC argument.
        for _ in range(3):
            if not isinstance(auth, dict) or auth.get("status") != "challenge":
                break
            sid = auth.get("session_id")
            require_read_capability("sber", "sber_session_info", {"session_id": sid})
            if challenge(auth) is not True:
                raise OperatorBlocked("human_challenge_required")
            auth = client.operator_auth("sber_auth_continue", {"session_id": sid, "wait_seconds": 0})
        _data(auth)
        sid = auth.get("session_id") or auth["data"].get("session_id")
        info = _data(client.call_tool("sber_session_info", {"session_id": sid, "check_live": False}))
        if info.get("profile") != profile:
            raise OperatorBlocked("profile_mismatch")
        if info.get("session_id") != sid or info.get("state") != "authenticated":
            raise OperatorBlocked("auth_required")
        products = _data(client.call_tool("sber_products", {"session_id": sid}))
        if any(not isinstance(products.get(key), list) for key in ("accounts", "cards")):
            raise OperatorBlocked("invalid_products")
        available = [p.get("operation_resource") for key in ("accounts", "cards") for p in products[key] if isinstance(p, dict)]
        if resource not in available:
            raise OperatorBlocked("resource_not_in_confirmed_products")
        args = {"session_id": sid, "resource": resource, "offset": 0, "limit": limit,
                "from_date": start.isoformat(), "to_date": end.isoformat()}
        require_read_capability("sber", "sber_operations_page", args)
        page = _data(client.call_tool("sber_operations_page", args))
        operations, cursor = page.get("operations"), page.get("next_offset")
        if (not isinstance(operations, list) or len(operations) > limit
                or type(page.get("count")) is not int or page["count"] != len(operations)
                or (cursor is not None and (type(cursor) is not int or cursor != limit))
                or any(not isinstance(op, dict) or not isinstance(op.get("id"), str)
                       or not isinstance(op.get("amount"), dict) or not isinstance(op.get("date"), str) for op in operations)):
            raise OperatorBlocked("invalid_page_structure")
        return {"status": "ok", "session": "ok", "profile_matches": True,
                "product_count": len(available), "page": "ok", "operation_count": len(operations),
                "has_next_page": cursor is not None, "structure": "valid", "writes_allowed": False}
    except (MCPError, OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise OperatorBlocked("operator_transport_or_challenge_failed") from None
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - sanitize every terminal transport cleanup failure
            # Cleanup is an external transport boundary; never expose its payload
            # or return successful auth/read evidence when cleanup is unverified.
            raise OperatorBlocked("operator_cleanup_failed") from None


class MockOperator:
    """Synthetic default CLI fixture, not a bank fallback."""

    def __init__(self) -> None:
        self.closed = False
        self.authenticated = False

    def operator_auth(self, name: str, args: dict[str, Any]) -> Any:
        if self.closed:
            raise OperatorBlocked("process_ended_auth_required")
        if name == "sber_auth_start":
            return {"status": "challenge", "session_id": "fixture-session", "details": {"expects": ["pin"]}}
        self.authenticated = True
        return {"status": "ok", "session_id": "fixture-session", "data": {}}

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        require_read_capability("sber", name, args)
        if self.closed:
            raise OperatorBlocked("process_ended_auth_required")
        if name == "sber_setup_status":
            data: dict[str, Any] = {"writes_allowed": False}
        elif not self.authenticated:
            raise OperatorBlocked("auth_required")
        elif name == "sber_session_info":
            data = {"profile": "fixture", "session_id": "fixture-session", "state": "authenticated"}
        elif name == "sber_products":
            data = {"accounts": [{"operation_resource": "account:fixture"}], "cards": []}
        else:
            data = {"operations": [], "count": 0, "next_offset": None}
        return {"status": "ok", "data": data}

    def close(self) -> None:
        self.closed = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Operator readiness rehearsal; default MOCK, no bank calls")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    try:
        # Installed Windows SDK has disabled private-file checks. Never turn
        # an acknowledgement flag into evidence of safe installation or ACLs.
        if args.live:
            raise OperatorBlocked("live_installation_and_os_isolation_not_accepted")
        result = smoke(MockOperator(), profile="fixture", resource="account:fixture",
                       start=date(2026, 1, 1), end=date(2026, 1, 2), limit=5,
                       consent=True, challenge=lambda _: True)
        print(json.dumps({"mode": "mock", **result}))
        return 0
    except OperatorBlocked:
        code = "live_installation_and_os_isolation_not_accepted" if args.live else "operator_workflow_failed"
        print(json.dumps({"status": "blocked", "code": code}))
        return 2
    except (MCPUnavailable, OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "blocked", "code": "operator_workflow_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
