"""Personal read-only Sber capture using the existing operator and intake."""
from __future__ import annotations

import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.finance.sber_intake import collect_history, digest
from jarvis.finance.sber_operator import OperatorBlocked, OperatorStdio
from jarvis.mcp.errors import MCPError
from jarvis.mcp.factory import _parse_command
from jarvis.mcp.stdio_client import minimal_subprocess_env

PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# The SDK reports "still waiting for the secret" with these shapes.
AWAITING_CODES = frozenset({"pin_required", "awaiting_pin", "awaiting_secret", "challenge"})
# Bounded wait after one PIN write: the SDK may consume the file asynchronously.
PIN_WAIT_ATTEMPTS = 6
PIN_WAIT_PAUSE = 5.0


def bank_profile() -> str | None:
    """Profile name shared by auth and history; env-overridable, never invented."""
    value = (os.environ.get("SBER_MCP_PROFILE") or "default").strip()
    return value if PROFILE_PATTERN.fullmatch(value) else None


def delivery_is_private(path: Path, home: Path) -> bool:
    """True only for a regular delivery file somewhere inside the SDK inbox.

    The SDK writes the PIN ticket one level below the inbox
    (``inbox/<session>/otp-*.txt``), so the check accepts a bounded nesting
    instead of a single flat directory, while still refusing any path that
    escapes the inbox or runs through a symlink.

    Windows paths are compared case- and separator-insensitively, because the
    SDK may echo the state directory with different casing than the environment.
    """
    try:
        raw = Path(path)
        if raw.is_symlink():
            return False
        ticket = Path(os.path.realpath(raw))
        inbox = Path(os.path.realpath(home / "inbox"))
    except OSError:
        return False
    if inbox.is_symlink():
        return False
    inbox_text = str(inbox)
    prefix = os.path.normcase(inbox_text + os.sep)
    if not os.path.normcase(str(ticket)).startswith(prefix):
        return False
    tail = str(ticket)[len(inbox_text) + 1:].split(os.sep)
    if not tail or len(tail) > 3:
        return False
    current = inbox
    for part in tail[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return False
    name = tail[-1]
    return bool(name) and name not in {".", ".."} and len(name) <= 128


def submit_pin(client: Any, challenge: dict[str, Any], pin: str, home: Path, *,
               sleep: Any = time.sleep, attempts: int = 6, pause: float = 5.0) -> tuple[dict[str, Any], Path | None]:
    """Write the owner PIN once, then wait for a terminal state.

    The PIN is submitted exactly once: the loop only re-reads the local MCP
    auth state (same session, same already-written file) because the SDK may
    consume the delivery file asynchronously. Repeat PIN values are never sent.
    """
    if not isinstance(pin, str) or not pin.isascii() or not re.fullmatch(r"[0-9]{5}", pin):
        return {"status": "auth_required", "code": "pin_format_invalid"}, None
    delivery = (challenge.get("details") or {}).get("delivery") or {}
    raw = delivery.get("path")
    session = challenge.get("session_id") or (challenge.get("data") or {}).get("session_id")
    if not isinstance(raw, str) or not isinstance(session, str) or not raw:
        return {"status": "auth_required", "code": "pin_delivery_missing"}, None
    ticket = Path(raw)
    if not delivery_is_private(ticket, home):
        # Never write a PIN outside the SDK inbox, and never echo the path.
        return {"status": "auth_required", "code": "pin_delivery_not_private"}, None
    ticket.write_text(pin, encoding="utf-8")
    result = client.operator_auth("sber_auth_continue",
        {"session_id": session, "wait_seconds": 0})
    for _ in range(max(0, attempts - 1)):
        if result.get("status") == "ok":
            break
        code = str(result.get("code") or "")
        if code not in AWAITING_CODES and str(result.get("next_action") or "") != "SUBMIT_SECRET":
            break
        sleep(pause)
        result = client.operator_auth("sber_auth_continue",
            {"session_id": session, "wait_seconds": 0})
    return result, ticket


def merge_history(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Replace refreshed windows as a whole, including removed pagination tails."""
    windows: dict[tuple[str, str, str], dict[str, Any]] = {}
    pages: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for packet in (old, new):
        for w in packet["windows"]:
            coordinate = (w["resource"], w["from_date"], w["to_date"])
            windows[coordinate] = dict(w)
            # A refreshed terminal window can contain fewer pages than before.
            # Keeping its old tail would resurrect no-longer-observed operations.
            pages = {key: page for key, page in pages.items() if key[:3] != coordinate}
        for p in packet["pages"]:
            a = p["request"]
            pages[(a["resource"], a["from_date"], a["to_date"], a["offset"])] = dict(p)
    ordered = list(windows)
    for key, p in pages.items():
        p["window"] = ordered.index(key[:3])
    return {**new, "scope": {**new["scope"], "start": min(old["scope"]["start"], new["scope"]["start"])},
        "windows": list(windows.values()), "pages": list(pages.values()),
        "coverage": "partial", "coverage_basis": "incremental_visible_history_not_financial_completeness"}


def capture_bank(start: date, end: date, *, previous: dict[str, Any] | None = None,
                 allow_pin: bool = True, pin: str | None = None) -> dict[str, Any]:
    """Restore once, capture once; never retry PIN, payments or uncertain calls."""
    from jarvis.core.config import settings
    home = Path(os.environ.get("SBER_MCP_HOME", ""))
    profile = bank_profile()
    if not home.is_absolute() or not settings.sber_mcp_command:
        return {"status": "not_configured", "code": "sber_mcp_not_configured",
                "action": "Настройте SBER_MCP_HOME и JARVIS_SBER_MCP_COMMAND."}
    if profile is None:
        return {"status": "not_configured", "code": "sber_profile_invalid",
                "action": "Имя профиля SBER_MCP_PROFILE должно быть A-Z, a-z, 0-9, _ или -."}
    env = minimal_subprocess_env()
    env["SBER_MCP_HOME"] = str(home)
    env["SBER_MCP_PROFILE"] = profile
    client = OperatorStdio(_parse_command(settings.sber_mcp_command), consent=True, env=env)
    submitted: Path | None = None
    stage = "auth_start"
    try:
        result = client.operator_auth("sber_auth_start", {"mode": "auto", "profile": profile})
        # No secrets in RPC, response, logs or stored captures. The SDK owns this inbox.
        # A PIN from the environment is only submitted when the owner explicitly opts in:
        # silent retries of a stale PIN can lock the real bank account.
        pin_value = pin
        if pin_value is None and os.environ.get("JARVIS_SBER_ALLOW_ENV_PIN", "").strip() in {"1", "true", "yes"}:
            pin_value = os.environ.get("SBER_MCP_PIN")
        if allow_pin and result.get("code") == "pin_required" and pin_value:
            stage = "pin_submit"
            result, submitted = submit_pin(client, result, pin_value, home,
                attempts=PIN_WAIT_ATTEMPTS, pause=PIN_WAIT_PAUSE)
        if result.get("status") != "ok":
            awaiting = str(result.get("code") or "") in AWAITING_CODES
            return {"status": "auth_required",
                    "code": result.get("code") or "auth_failed",
                    "awaiting_secret": awaiting,
                    "next_action": result.get("next_action"),
                    "action": "Восстановите банковскую сессию локально; секреты в чат не вводите."}
        sid = result.get("session_id") or result.get("data", {}).get("session_id")
        stage = "products"
        products = client.call_tool("sber_products", {"session_id": sid, "force_update": True})
        if products.get("status") != "ok":
            return {"status": "unavailable", "code": products.get("code", "products_failed"),
                    "stage": stage}
        observed_at = datetime.now(UTC).isoformat()
        old = previous.get("history") if previous else None
        stage = "history"
        try:
            if old and any(not w["terminal"] for w in old["windows"]):
                checkpoint = {k: v for k, v in old.items() if k != "evidence_sha256"}
                checkpoint["evidence_sha256"] = digest(checkpoint)
                packet = collect_history(client, session_id=sid, expected_profile=profile,
                    start=date.fromisoformat(old["scope"]["start"]), end=date.fromisoformat(old["scope"]["end"]),
                    checkpoint=checkpoint)
            else:
                overlap = max(start, date.fromisoformat(old["scope"]["end"]) - timedelta(days=6)) if old else start
                packet = collect_history(client, session_id=sid, expected_profile=profile, start=overlap, end=end)
                if old:
                    packet = merge_history(old, packet)
        except ValueError as exc:
            # Sign-in may have succeeded while the history scope/profile did not.
            # Keep that distinct from a PIN problem and expose the local reason only.
            return {"status": "unavailable", "code": "history_scope_invalid",
                    "auth_ok": True, "detail": str(exc)[:80]}
        if packet.get("status") != "read_only_evidence" or packet.get("source_error"):
            # Never commit a snapshot when the history read failed internally:
            # that would look like a successful sync with unread operations.
            return {"status": "unavailable", "code": "history_unavailable", "stage": "history",
                    "auth_ok": True, "source_error": packet.get("source_error"),
                    "pages": len(packet.get("pages") or [])}
        # Session handles are transport secrets, not financial evidence.
        for page in packet.get("pages", []):
            page["request"].pop("session_id", None)
        packet.pop("evidence_sha256", None)
        return {"status": "captured", "observed_at": observed_at, "products": products["data"], "history": packet}
    except (MCPError, OperatorBlocked, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        # Static stage + sanitized reason: no bank data, no secret, no traceback.
        safe = re.sub(r"[^A-Za-z0-9_:. -]", "", str(exc))[:80]
        return {"status": "unavailable", "code": "capture_failed", "stage": stage,
                "detail": f"{type(exc).__name__}: {safe}".strip()}
    finally:
        try:
            client.close()
        finally:
            if submitted is not None and submitted.exists():
                submitted.write_text("", encoding="utf-8")
                submitted.unlink()
