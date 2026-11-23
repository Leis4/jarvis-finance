"""Scoped DPAPI SDK codec; configuration is not permission to contact a bank."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.finance.windows_session_store import (
    SessionStorageUnavailable,
    load_state,
    save_state,
)
from jarvis.mcp.errors import MCPPolicyDenied

_ROOT: ContextVar[Path | None] = ContextVar("sber_disposable_root", default=None)
_INBOX: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar("sber_disposable_inbox", default=None)
_CONFIG: ContextVar[dict[str, Any] | None] = ContextVar("sber_operator_config", default=None)
_REJECTIONS: ContextVar[dict[str, int] | None] = ContextVar("sber_operator_rejections", default=None)

READ_CAPABILITIES = ("sber_session_info", "sber_products", "sber_operations_page")


def issue_operator_authorization(*, start: date, end: date, confirm: Any, auth_mode: str = "cold") -> Path:
    """Issue a leased session from the native human dialog; test mode stays distinct."""
    import msvcrt
    import sys
    config, root, inbox = _CONFIG.get(), _ROOT.get(), _INBOX.get()
    if config is None or root is None or inbox is None or not 0 <= (end - start).days <= 31 or auth_mode not in {"cold", "auto", "pin"}:
        raise SessionStorageUnavailable("Operator authorization unavailable")
    synthetic = config["synthetic"] is True
    if not synthetic and ("pytest" in sys.modules or os.environ.get("TEST_SBER_PUBLIC_WHEELS")):
        raise SessionStorageUnavailable("Genuine consent forbidden in tests")
    if not synthetic:
        from jarvis.finance.sber_captcha_viewer import confirm_operator_read
        confirm = confirm_operator_read
    notice = {"artifact_identity": config["artifact_identity"], "profile": config["configuration_id"],
              "mode": "TEST_READ" if synthetic else "HUMAN_READ", "from_date": start.isoformat(),
              "to_date": end.isoformat(), "limit": 5, "ttl_seconds": 300, "auth_mode": auth_mode}
    if confirm(notice) is not True:
        raise SessionStorageUnavailable("Operator authorization cancelled")
    nonce = secrets.token_hex(16)
    path = root / ("operator-read-" + nonce + ".dpapi")
    clock = datetime.now(UTC)
    lease = path.with_suffix(".lease").open("x+b")
    try:
        lease.write(b"0")
        lease.flush()
        lease.seek(0)
        msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
        write_private(path, {"format": "jarvis-operator-read-v1", "configuration": config,
            **notice, "nonce": nonce, "issued": clock.isoformat(), "expires": (clock + timedelta(minutes=5)).isoformat(),
            "resource": None, "selected": False, "closed": False})
        snapshot = json.loads(read_private(path))
        immutable = {k: v for k, v in snapshot.items() if k not in {"resource", "selected", "closed"}}
        write_private(path.with_suffix(".scope"), {"sha256": hashlib.sha256(json.dumps(immutable, sort_keys=True).encode()).hexdigest()})
        inbox["operator-read-" + nonce] = {"path": path, "lease": lease}
    except BaseException:
        lease.close()
        raise
    return path


def operator_authorization(path: Path, *, synthetic: bool) -> dict[str, Any]:
    """Check live issuer, current binding and fixed bounded policy in either process."""
    try:
        body = json.loads(read_private(path))
        issued, expires = datetime.fromisoformat(body["issued"]), datetime.fromisoformat(body["expires"])
        expected = {"format", "configuration", "artifact_identity", "profile", "mode", "from_date", "to_date",
                    "limit", "ttl_seconds", "nonce", "issued", "expires", "resource", "selected", "closed", "auth_mode"}
        immutable = {k: v for k, v in body.items() if k not in {"resource", "selected", "closed"}}
        if hashlib.sha256(json.dumps(immutable, sort_keys=True).encode()).hexdigest() != json.loads(read_private(path.with_suffix(".scope")))["sha256"]:
            raise ValueError
        config = _CONFIG.get()
        if (set(body) != expected or config is None or body["configuration"] != config
                or type(synthetic) is not bool or config["synthetic"] is not synthetic
                or body["format"] != "jarvis-operator-read-v1"
                or body["mode"] != ("TEST_READ" if synthetic else "HUMAN_READ")
                or body["artifact_identity"] != config["artifact_identity"] or body["profile"] != config["configuration_id"]
                or not re.fullmatch(r"[a-f0-9]{32}", body["nonce"])
                or path.name != "operator-read-" + body["nonce"] + ".dpapi"
                or body["closed"] is not False or type(body["selected"]) is not bool
                or body["auth_mode"] not in {"cold", "auto", "pin"}
                or type(body["limit"]) is not int or body["limit"] != 5 or body["ttl_seconds"] != 300
                or issued.tzinfo is None or expires.tzinfo is None or not issued <= datetime.now(UTC) < expires
                or (expires - issued).total_seconds() != 300
                or not 0 <= (date.fromisoformat(body["to_date"]) - date.fromisoformat(body["from_date"])).days <= 31
                or (body["selected"] and not re.fullmatch(r"(?:ct-account|account|card):[A-Za-z0-9_-]{1,128}", body["resource"]))
                or (not body["selected"] and body["resource"] is not None)):
            raise ValueError
        _issuer_alive(path)
        return dict(body)
    except (ValueError, TypeError, KeyError, OSError):
        raise SessionStorageUnavailable("Operator authorization unavailable") from None


def select_operator_resource(path: Path, resource: str, *, confirm: Any) -> None:
    """Fix one human-selected resource without extending the original deadline."""
    config = _CONFIG.get()
    if config is None:
        raise SessionStorageUnavailable("Operator binding required")
    body = operator_authorization(path, synthetic=config["synthetic"])
    if config["synthetic"] is False:
        from jarvis.finance.sber_captcha_viewer import confirm_operator_read
        confirm = confirm_operator_read
    if body["selected"] or not re.fullmatch(r"(?:ct-account|account|card):[A-Za-z0-9_-]{1,128}", resource):
        raise SessionStorageUnavailable("Operator resource unavailable")
    if confirm({**body, "resource": resource}) is not True:
        body["closed"] = True
        write_private(path, body)
        raise SessionStorageUnavailable("Operator selection cancelled")
    body.update(resource=resource, selected=True)
    write_private(path, body)


def close_operator_authorization(path: Path) -> None:
    """Revoke an owned permit even after expiry; retain consumption tombstones."""
    body = json.loads(read_private(path))
    body["closed"] = True
    write_private(path, body)


def issue_demo_read_authorization(*, resource: str, start: date, end: date,
                                  now: datetime | None = None) -> Path:
    """Issue synthetic-only scope evidence; never a genuine-bank authorization."""
    config, root = _CONFIG.get(), _ROOT.get()
    clock = now or datetime.now(UTC)
    if (config is None or root is None or config["synthetic"] is not True
            or clock.tzinfo is None or not re.fullmatch(r"(?:account|ct-account|card):[A-Za-z0-9_-]{1,128}", resource)
            or not 0 <= (end - start).days <= 31):
        raise SessionStorageUnavailable("Synthetic bounded authorization required")
    nonce = secrets.token_hex(16)
    path = root / ("read-" + nonce + ".dpapi")
    write_private(path, {"format": "jarvis-read-authorization-v1", "mode": "DEMO",
        "configuration": config, "nonce": nonce, "issued": clock.isoformat(),
        "expires": (clock + timedelta(minutes=5)).isoformat(),
        "capabilities": list(READ_CAPABILITIES), "resource": resource,
        "from_date": start.isoformat(), "to_date": end.isoformat(), "limit": 5,
        "offset": 0, "session_id": None, "operations_used": False, "closed": False})
    write_private(path.with_suffix(".scope"), {"sha256": hashlib.sha256(read_private(path)).hexdigest()})
    return path


def consume_demo_read_authorization(path: Path, session_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Claim once atomically, retaining a tombstone across process restart."""
    body = _read_authorization(path, now=now)
    if body["session_id"] is not None or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise SessionStorageUnavailable("Read authorization unavailable")
    try:
        descriptor = os.open(_path(path).with_suffix(".claim"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        raise SessionStorageUnavailable("Read authorization unavailable") from None
    os.close(descriptor)
    body["session_id"] = session_id
    write_private(path, body)
    write_private(path.with_suffix(".scope"), {"sha256": hashlib.sha256(read_private(path)).hexdigest()})
    return dict(body)


def _read_authorization(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Validate all immutable scope fields and current generation, fail closed."""
    clock = now or datetime.now(UTC)
    try:
        body = json.loads(read_private(path))
        if hashlib.sha256(read_private(path)).hexdigest() != json.loads(read_private(path.with_suffix(".scope")))["sha256"]:
            raise ValueError
        issued, expires = datetime.fromisoformat(body["issued"]), datetime.fromisoformat(body["expires"])
        expected = {"format", "mode", "configuration", "nonce", "issued", "expires", "capabilities", "resource",
                    "from_date", "to_date", "limit", "offset", "session_id", "operations_used", "closed"}
        if (set(body) != expected or body["format"] != "jarvis-read-authorization-v1" or body["mode"] != "DEMO"
                or body["configuration"] != _CONFIG.get() or body["configuration"]["synthetic"] is not True
                or not re.fullmatch(r"[a-f0-9]{32}", body["nonce"])
                or path.name != "read-" + body["nonce"] + ".dpapi"
                or clock.tzinfo is None or issued.tzinfo is None or expires.tzinfo is None
                or not issued <= clock < expires or not 0 < (expires - issued).total_seconds() <= 300
                or body["capabilities"] != list(READ_CAPABILITIES)
                or not re.fullmatch(r"(?:account|ct-account|card):[A-Za-z0-9_-]{1,128}", body["resource"])
                or not 0 <= (date.fromisoformat(body["to_date"]) - date.fromisoformat(body["from_date"])).days <= 31
                or type(body["limit"]) is not int or body["limit"] != 5
                or type(body["offset"]) is not int or body["offset"] != 0
                or type(body["operations_used"]) is not bool or body["closed"] is not False):
            raise ValueError
        return dict(body)
    except (ValueError, TypeError, KeyError, OSError):
        raise SessionStorageUnavailable("Read authorization unavailable") from None


def reserve_demo_read(path: Path, *, session_id: str, tool: str, arguments: Mapping[str, Any],
                      now: datetime | None = None) -> None:
    """Reserve the sole page before an outgoing call; failure never restores it."""
    body = _read_authorization(path, now=now)
    if body["session_id"] != session_id or not path.with_suffix(".claim").is_file() or tool not in READ_CAPABILITIES:
        raise SessionStorageUnavailable("Read authorization unavailable")
    if tool == "sber_operations_page":
        expected = {"session_id": session_id, "resource": body["resource"], "from_date": body["from_date"],
                    "to_date": body["to_date"], "limit": 5, "offset": 0}
        if dict(arguments) != expected or type(arguments.get("limit")) is not int or body["operations_used"]:
            raise SessionStorageUnavailable("Read authorization unavailable")
        # Exclusive page claim also prevents concurrent processes reserving twice.
        try:
            descriptor = os.open(_path(path).with_suffix(".page"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            raise SessionStorageUnavailable("Read authorization unavailable") from None
        os.close(descriptor)
        body["operations_used"] = True
        write_private(path, body)
        write_private(path.with_suffix(".scope"), {"sha256": hashlib.sha256(read_private(path)).hexdigest()})
    elif dict(arguments) != ({"session_id": session_id, "check_live": False} if tool == "sber_session_info" else {"session_id": session_id}):
        raise SessionStorageUnavailable("Read authorization unavailable")


def revoke_demo_read_authorization(path: Path) -> None:
    """Close this receipt without deleting its replay tombstones."""
    body = _read_authorization(path)
    body["closed"] = True
    write_private(path, body)


def _operator_root(root: Path) -> Path:
    if not root.is_absolute():
        raise SessionStorageUnavailable("Explicit operator path required")
    for part in (root, *root.parents):
        if part.exists() and (part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise SessionStorageUnavailable("Operator path redirected")
        if (part / ".git").exists():
            raise SessionStorageUnavailable("Operator state outside Git required")
    resolved = root.resolve()
    if resolved.is_relative_to(Path(__file__).resolve().parents[1]):
        raise SessionStorageUnavailable("Operator state outside source required")
    return resolved


def _configuration(root: Path, configuration_id: str) -> dict[str, Any]:
    try:
        body = json.loads(load_state(root / "operator-config.dpapi"))
        if (set(body) != {"format", "configuration_id", "artifact_identity", "root_binding", "generation", "synthetic"}
                or body["format"] != "jarvis-operator-config-v1" or body["configuration_id"] != configuration_id
                or body["root_binding"] != hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
                or type(body["synthetic"]) is not bool
                or not re.fullmatch(r"[a-f0-9]{64}", body["artifact_identity"])
                or not re.fullmatch(r"[a-f0-9]{32}", body["generation"])):
            raise ValueError
        return dict(body)
    except (OSError, ValueError, TypeError, KeyError):
        raise SessionStorageUnavailable("Operator configuration unavailable") from None


def create_operator_state(root: Path, *, configuration_id: str, artifact_identity: str, synthetic: bool) -> dict[str, Any]:
    """Enroll a NEW owned location; never adopt an existing bank profile."""
    root = _operator_root(root)
    if (root.exists() or not re.fullmatch(r"[a-z0-9_-]{1,32}", configuration_id)
            or not re.fullmatch(r"[a-f0-9]{64}", artifact_identity) or type(synthetic) is not bool):
        raise SessionStorageUnavailable("New operator configuration required")
    root.mkdir(parents=True)
    body = {"format": "jarvis-operator-config-v1", "configuration_id": configuration_id,
            "artifact_identity": artifact_identity, "synthetic": synthetic,
            "generation": secrets.token_hex(16),
            "root_binding": hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()}
    save_state(root / "operator-config.dpapi", json.dumps(body, sort_keys=True).encode())
    return {"configuration_id": configuration_id, "synthetic": synthetic,
            "status": "disconnected", "database_modified": False}


@contextmanager
def operator_binding(root: Path, *, configuration_id: str, artifact_identity: str) -> Iterator[None]:
    """Bind explicit enrolled state; does not grant live auth or effective ACL proof."""
    root = _operator_root(root)
    config = _configuration(root, configuration_id)
    if config["artifact_identity"] != artifact_identity:
        raise SessionStorageUnavailable("Operator artifact mismatch")
    with _binding(root, config):
        yield


def _private_save(path: Path, raw: bytes) -> None:
    target = _path(path)
    config = _CONFIG.get()
    if config is not None:
        root = _ROOT.get()
        if root is None:
            raise SessionStorageUnavailable("Operator binding required")
        raw = json.dumps({"format": "jarvis-operator-private-v1", "configuration": config,
                          "relative_path": target.relative_to(root).as_posix(),
                          "payload": base64.b64encode(raw).decode()}, sort_keys=True).encode()
    save_state(target, raw)


def disconnect_operator(root: Path, *, configuration_id: str, artifact_identity: str) -> None:
    """After caller closes its process, remove only this configuration's known state."""
    with operator_binding(root, configuration_id=configuration_id, artifact_identity=artifact_identity):
        for relative in (f"profiles/{configuration_id}.json", "credentials.json", "deviceprint.txt", "antifraud-deviceprint.txt"):
            target = _path(root / relative)
            if target.exists():
                read_private(target)  # Refuse foreign/unbound files before unlinking.
                target.unlink()
        config = dict(_CONFIG.get() or {})
        config["generation"] = secrets.token_hex(16)
        save_state(root / "operator-config.dpapi", json.dumps(config, sort_keys=True).encode())


def deny_bank_write() -> None:
    """Refuse SDK mutation entrypoints before transport, including alternate calls."""
    raise MCPPolicyDenied("Banking writes forbidden")


def _path(path: Path) -> Path:
    root = _ROOT.get()
    if root is None or not path.is_absolute():
        raise SessionStorageUnavailable("Disposable SDK binding required")
    config = _CONFIG.get()
    if config is not None and _configuration(root, config["configuration_id"]) != config:
        raise SessionStorageUnavailable("Operator configuration revoked")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise SessionStorageUnavailable("Disposable SDK path denied")
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise SessionStorageUnavailable("Disposable SDK path denied")
        if part == root:
            break
    return resolved


@contextmanager
def disposable_binding(root: Path) -> Iterator[None]:
    """Explicit fixture context; not a live security receipt or SDK installation."""
    if root.is_symlink() or getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
        raise SessionStorageUnavailable("Disposable SDK binding required")
    root = root.resolve(strict=True)
    if not root.name.startswith("jarvis-v6-sdk-") or not root.is_dir() or not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise SessionStorageUnavailable("Disposable SDK binding required")
    with _binding(root, None):
        yield


@contextmanager
def _binding(root: Path, config: dict[str, Any] | None) -> Iterator[None]:
    token = _ROOT.set(root)
    config_token = _CONFIG.set(config)
    rejection_token = _REJECTIONS.set({})
    inbox_token = _INBOX.set({})
    try:
        yield
    finally:
        inbox = _INBOX.get() or {}
        try:
            for value in list(inbox.values()):
                _remove_answer(value)
        finally:
            _INBOX.reset(inbox_token)
            _CONFIG.reset(config_token)
            _REJECTIONS.reset(rejection_token)
            _ROOT.reset(token)


def read_private(path: Path, *, require_private: bool = True) -> bytes:
    """Decode ciphertext before the actual SDK JSON/text parser sees bytes."""
    if require_private is not True:
        raise SessionStorageUnavailable("Private codec cannot be disabled")
    target = _path(path)
    raw = load_state(target)
    config = _CONFIG.get()
    if config is not None:
        root = _ROOT.get()
        if root is None:
            raise SessionStorageUnavailable("Operator binding required")
        try:
            body = json.loads(raw)
            if (set(body) != {"format", "configuration", "relative_path", "payload"}
                    or body["format"] != "jarvis-operator-private-v1" or body["configuration"] != config
                    or body["relative_path"] != target.relative_to(root).as_posix()):
                raise ValueError
            raw = base64.b64decode(body["payload"], validate=True)
        except (ValueError, KeyError, TypeError):
            raise SessionStorageUnavailable("Operator private state unavailable") from None
    return raw


def write_private(path: Path, payload: Mapping[str, Any]) -> None:
    """Serialize the SDK's own payload, then publish ciphertext atomically."""
    try:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise SessionStorageUnavailable("SDK state encoding unavailable") from None
    _private_save(path, raw)


def private_dir(path: Path) -> None:
    """Accept only an existing disposable directory; no ACL safety assertion."""
    if not _path(path).is_dir():
        raise SessionStorageUnavailable("Disposable SDK directory unavailable")


def challenge_active(ticket: Path) -> None:
    """Validate the active encrypted issuer ticket without disclosing secret answers."""
    target = _path(ticket)
    try:
        body = json.loads(read_private(target))
        expires = datetime.fromisoformat(body["expires"])
        if (expires.tzinfo is None or expires <= datetime.now(UTC) or body["cancelled"] or body["lines"]
                or target.name != "otp-" + body["nonce"] + ".dpapi" or target.with_suffix(".claim").exists()):
            raise ValueError
        _issuer_alive(target)
    except (ValueError, TypeError, KeyError, OSError):
        raise SessionStorageUnavailable("Active challenge unavailable") from None


async def advance_with_retry(reg: Any, state: Any, lines: list[str], advance: Any) -> Any:
    """Re-arm only a rejected OTP within original deadline and finite local budget."""
    from importlib import import_module
    server = import_module("sber_unofficial_mcp.server")
    deadline = state.challenge_expires
    try:
        return await advance(reg, state, lines)
    except server.PinAuthError as error:
        attempts = _REJECTIONS.get()
        if attempts is None:
            raise
        attempts[state.session_id] = attempts.get(state.session_id, 0) + 1
        if (error.code != "wrong_code" or state.state != "awaiting_otp" or attempts[state.session_id] >= 3
                or error.remaining_attempts == 0 or deadline is None or deadline <= datetime.now(UTC)):
            raise
        result = server._arm(reg, state, "otp_required", ["otp"], extra={"problem": "otp_rejected"})
        state.challenge_expires = min(deadline, state.challenge_expires)
        sync_challenge_expiry(state)
        return result


def ensure_fingerprint(home: Path, name: str, generator: Any) -> str:
    """Preserve SDK generation/text contract using encrypted disposable storage."""
    path = _path(home / name)
    if path.exists():
        return read_private(path).decode("utf-8")
    value = generator()
    if not isinstance(value, str) or not 0 < len(value) <= 65536:
        raise SessionStorageUnavailable("SDK fingerprint unavailable")
    _private_save(path, value.encode("utf-8"))
    return value


async def arm_captcha(reg: Any, state: Any, error: Any, *, audio: bool = False) -> dict[str, Any]:
    """Use SDK captcha fetch/transition in fixtures; keep image bytes encrypted."""
    state.state = "awaiting_captcha"
    url = error.audio_url if audio else error.image_url
    if not url or state.auth is None:
        raise SessionStorageUnavailable("SDK captcha unavailable")
    data = await state.auth.fetch_captcha(url)
    if not isinstance(data, bytes) or not 0 < len(data) <= 1024 * 1024:
        raise SessionStorageUnavailable("SDK captcha unavailable")
    from importlib import import_module
    server = import_module("sber_unofficial_mcp.server")
    expects = ["captcha_code"] if state.mode == "cold" else ["pin", "captcha_code"]
    result = dict(server._arm(reg, state, "captcha_required", expects))
    artifact = _path(reg.home / ("captcha-" + secrets.token_hex(16) + ".dpapi"))
    _private_save(artifact, data)
    state.captcha_artifact, state.captcha_challenge = artifact, error
    inbox = _INBOX.get() or {}
    inbox[state.session_id]["artifact"] = artifact
    # Encrypted, nonce-bound receipt; never accept a caller-selected image path.
    write_private(artifact.with_suffix(".receipt"), {"ticket": str(state.inbox_path),
                  "nonce": inbox[state.session_id]["nonce"], "expires": inbox[state.session_id]["expires"].isoformat()})
    result["details"]["artifact_path"] = str(artifact)
    return result


def _valid_answers(lines: Any, expects: list[str]) -> bool:
    if not isinstance(lines, list) or len(lines) != len(expects) or len(lines) > 2:
        return False
    return all(isinstance(value, str) and value.isascii() and
               (value.isalnum() and 1 <= len(value) <= 32 if kind == "captcha_code" else
                value.isdigit() and 4 <= len(value) <= 12) for value, kind in zip(lines, expects, strict=True))


def active_captcha(ticket: Path, artifact: Path) -> tuple[str, bytes, datetime]:
    """Read only an active nonce-bound generated image; no arbitrary original path."""
    import hashlib
    import re

    target, image = _path(ticket), _path(artifact)
    try:
        body = json.loads(read_private(target))
        receipt = json.loads(read_private(image.with_suffix(".receipt")))
        expires = datetime.fromisoformat(body["expires"])
        if (not re.fullmatch(r"captcha-[0-9a-f]{32}\.dpapi", image.name)
                or expires.tzinfo is None or datetime.now(UTC) >= expires
                or body["cancelled"] or body["lines"] or "captcha_code" not in body["expects"]
                or target.name != "otp-" + body["nonce"] + ".dpapi"
                or receipt != {"ticket": str(target), "nonce": body["nonce"], "expires": body["expires"]}
                or target.with_suffix(".claim").exists()):
            raise ValueError
        _issuer_alive(target)
        raw = read_private(image)
        if not 0 < len(raw) <= 1_048_576:
            raise ValueError
        return hashlib.sha256(body["nonce"].encode()).hexdigest()[:8], raw, expires
    except (OSError, ValueError, TypeError, KeyError):
        raise SessionStorageUnavailable("Active captcha unavailable") from None


def arm_inbox(reg: Any, state: Any, expects: list[str]) -> Path:
    """Arm an encrypted single-use answer for the disposable operator only."""
    if not expects or len(expects) > 2 or any(e not in {"pin", "otp", "new_pin", "new_pin_confirm", "captcha_code"} for e in expects):
        raise SessionStorageUnavailable("Unsupported SDK challenge")
    private_dir(reg.home)
    clear_inbox(reg, state)
    inbox = _INBOX.get()
    if inbox is None:
        raise SessionStorageUnavailable("Disposable SDK binding required")
    folder = _path(reg.home / "inbox")
    folder.mkdir(exist_ok=True)
    nonce = secrets.token_hex(16)
    path = folder / ("otp-" + nonce + ".dpapi")
    expires = datetime.now(UTC) + timedelta(minutes=5)
    # This lock belongs to the issuer process and disappears on crash/exit.
    import msvcrt
    lease = path.with_suffix(".lease").open("x+b")
    try:
        lease.write(b"0")
        lease.flush()
        lease.seek(0)
        msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
        write_private(path, {"nonce": nonce, "lines": [], "expires": expires.isoformat(),
                             "expects": expects, "cancelled": False})
    except (OSError, ValueError):
        lease.close()
        path.with_suffix(".lease").unlink(missing_ok=True)
        raise SessionStorageUnavailable("SDK challenge creation unavailable") from None
    inbox[state.session_id] = {"nonce": nonce, "path": path, "lease": lease, "expires": expires}
    state.inbox_path, state.inbox_fd, state.inbox_inode = path, None, None
    state.expects, state.consumed = list(expects), False
    return path


def submit_inbox(path: Path, lines: list[str]) -> None:
    """Publish once from a separate local operator; never an MCP secret argument."""
    _submit_answer(path, lines, cancelled=False)


def cancel_inbox(path: Path) -> None:
    """Cancel an active answer without sending a secret or initiating auth."""
    _submit_answer(path, [], cancelled=True)


def sync_challenge_expiry(state: Any) -> None:
    """Bind the encrypted operator ticket to the SDK's shorter deadline."""
    entry = (_INBOX.get() or {}).get(state.session_id)
    if entry is None:
        raise SessionStorageUnavailable("SDK challenge unavailable")
    expires = min(entry["expires"], state.challenge_expires)
    body = json.loads(read_private(entry["path"]))
    body["expires"] = expires.isoformat()
    write_private(entry["path"], body)
    entry["expires"] = expires


def _issuer_alive(path: Path) -> None:
    import msvcrt
    try:
        with _path(path.with_suffix(".lease")).open("r+b") as lease:
            try:
                msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in {13, 36}:  # Another handle owns the byte lock.
                    return
                raise
            msvcrt.locking(lease.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        raise SessionStorageUnavailable("SDK challenge issuer unavailable") from None
    raise SessionStorageUnavailable("SDK challenge issuer unavailable")


def _submit_answer(path: Path, lines: list[str], *, cancelled: bool) -> None:
    target = _path(path)
    try:
        body = json.loads(read_private(target))
        expires = datetime.fromisoformat(body["expires"])
        if expires.tzinfo is None or datetime.now(UTC) >= expires or target.name != "otp-" + body["nonce"] + ".dpapi" or body["lines"] or body["cancelled"]:
            raise ValueError
    except (ValueError, TypeError, KeyError, OSError):
        raise SessionStorageUnavailable("SDK challenge unavailable") from None
    if not cancelled and not _valid_answers(lines, body["expects"]):
        raise SessionStorageUnavailable("SDK challenge answer invalid")
    _issuer_alive(target)
    claim = _path(target.with_suffix(".claim"))
    try:
        descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    except OSError:
        raise SessionStorageUnavailable("SDK challenge already submitted") from None
    try:
        body.update(lines=lines, cancelled=cancelled)
        write_private(target, body)
    except (OSError, ValueError):
        claim.unlink(missing_ok=True)
        raise SessionStorageUnavailable("SDK challenge submission unavailable") from None


def read_inbox(reg: Any, state: Any) -> list[str] | None:
    """Consume/delete before the SDK auth callback; corruption never gets retried."""
    entry = (_INBOX.get() or {}).get(state.session_id)
    if entry is None or state.consumed:
        raise SessionStorageUnavailable("SDK challenge unavailable")
    expiry = state.challenge_expires
    if datetime.now(UTC) >= entry["expires"] or (expiry is not None and datetime.now(UTC) >= expiry):
        clear_inbox(reg, state)
        raise SessionStorageUnavailable("SDK challenge expired")
    try:
        body = json.loads(read_private(entry["path"]))
        if not isinstance(body, dict) or set(body) != {"nonce", "lines", "expires", "expects", "cancelled"} or body["nonce"] != entry["nonce"] or body["expires"] != entry["expires"].isoformat() or body["expects"] != state.expects:
            raise ValueError
        if body["cancelled"]:
            clear_inbox(reg, state)
            raise SessionStorageUnavailable("SDK challenge cancelled")
        lines = body["lines"]
        if lines and not _valid_answers(lines, state.expects):
            raise ValueError
        submitted = entry["path"].with_suffix(".claim").is_file()
        if not lines and not submitted:
            return None
        if not lines or not submitted:
            raise ValueError
    except (ValueError, OSError):
        clear_inbox(reg, state)
        raise SessionStorageUnavailable("SDK challenge invalid") from None
    state.consumed = True
    clear_inbox(reg, state)
    return list(lines)


def clear_inbox(reg: Any, state: Any) -> None:
    """Remove only a recorded adapter-owned challenge; no directory sweeping."""
    inbox = _INBOX.get()
    if inbox is None:
        raise SessionStorageUnavailable("Disposable SDK binding required")
    entry = inbox.get(state.session_id)
    if entry is not None:
        _remove_answer(entry)
        del inbox[state.session_id]
    state.inbox_path, state.inbox_fd, state.inbox_inode = None, None, None
    state.captcha_artifact = None


def _remove_answer(entry: dict[str, Any]) -> None:
    try:
        _path(entry["path"]).unlink(missing_ok=True)
        _path(entry["path"].with_suffix(".claim")).unlink(missing_ok=True)
        if entry.get("artifact") is not None:
            _path(entry["artifact"]).unlink(missing_ok=True)
            _path(entry["artifact"].with_suffix(".receipt")).unlink(missing_ok=True)
    finally:
        entry["lease"].close()
        _path(entry["path"].with_suffix(".lease")).unlink(missing_ok=True)
