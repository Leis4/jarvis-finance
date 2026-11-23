from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any

from jarvis.finance.db import FinanceDB

SECURITY_SETTING = "security"
SETUP_TOKEN_SETTING = "first_run_setup_token"
CSRF_SECRET_SETTING = "csrf_secret"
SESSION_SECRET_SETTING = "session_secret"
PBKDF2_SHA256_ITERATIONS = 600_000
ARGON2ID_TIME_COST = 3
ARGON2ID_MEMORY_KIB = 46_080
ARGON2ID_PARALLELISM = 1
LOCAL_BIND_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


@dataclass(frozen=True)
class PasswordHash:
    algorithm: str
    iterations: int
    salt_b64: str
    digest_b64: str
    encoded: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "iterations": self.iterations,
            "salt_b64": self.salt_b64,
            "digest_b64": self.digest_b64,
            "encoded": self.encoded,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PasswordHash:
        return cls(
            algorithm=str(payload.get("algorithm", "pbkdf2_sha256")),
            iterations=int(payload.get("iterations", PBKDF2_SHA256_ITERATIONS)),
            salt_b64=str(payload.get("salt_b64", "")),
            digest_b64=str(payload.get("digest_b64", "")),
            encoded=str(payload.get("encoded", "")),
        )


def secure_token(byte_length: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(byte_length)).decode("ascii").rstrip("=")


def _argon2_available() -> bool:
    try:
        import argon2.low_level  # type: ignore[import-not-found] # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def hash_password_argon2id(password: str) -> PasswordHash:
    """Hash a password with Argon2id when argon2-cffi is installed.

    Argon2id is the preferred password hashing family in the current OWASP
    guidance. It remains an optional dependency so the local MVP can still run
    without native wheels; when unavailable the caller should fall back to the
    PBKDF2 floor below.
    """
    if not password:
        raise ValueError("Password cannot be empty")
    try:
        from argon2 import PasswordHasher
    except Exception as exc:  # pragma: no cover - optional dependency path
        raise RuntimeError("Argon2id password hashing requires: pip install argon2-cffi") from exc
    encoded = str(
        PasswordHasher(
            time_cost=ARGON2ID_TIME_COST,
            memory_cost=ARGON2ID_MEMORY_KIB,
            parallelism=ARGON2ID_PARALLELISM,
        ).hash(password)
    )
    return PasswordHash(algorithm="argon2id", iterations=ARGON2ID_TIME_COST, salt_b64="", digest_b64="", encoded=encoded)


def hash_password_pbkdf2(password: str, *, iterations: int = PBKDF2_SHA256_ITERATIONS, salt: bytes | None = None) -> PasswordHash:
    if not password:
        raise ValueError("Password cannot be empty")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return PasswordHash(
        algorithm="pbkdf2_sha256",
        iterations=iterations,
        salt_b64=base64.b64encode(salt).decode("ascii"),
        digest_b64=base64.b64encode(digest).decode("ascii"),
    )


def hash_password(password: str, *, iterations: int = PBKDF2_SHA256_ITERATIONS, salt: bytes | None = None, prefer_argon2id: bool = True) -> PasswordHash:
    if prefer_argon2id and salt is None and iterations == PBKDF2_SHA256_ITERATIONS and _argon2_available():
        return hash_password_argon2id(password)
    return hash_password_pbkdf2(password, iterations=iterations, salt=salt)


def verify_password(password: str, stored: PasswordHash) -> bool:
    if stored.algorithm == "argon2id":
        if not stored.encoded:
            return False
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import (  # type: ignore[import-not-found]
                VerificationError,
            )
        except Exception:  # noqa: BLE001
            return False
        try:
            return bool(PasswordHasher().verify(stored.encoded, password))
        except VerificationError:
            return False
        except Exception:  # noqa: BLE001
            return False
    if stored.algorithm != "pbkdf2_sha256":
        return False
    try:
        salt = base64.b64decode(stored.salt_b64.encode("ascii"))
        expected = base64.b64decode(stored.digest_b64.encode("ascii"))
    except Exception:  # noqa: BLE001
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, stored.iterations)
    return hmac.compare_digest(actual, expected)


def password_hash_needs_upgrade(stored: PasswordHash) -> bool:
    if stored.algorithm == "argon2id":
        return False
    return stored.algorithm != "pbkdf2_sha256" or stored.iterations < PBKDF2_SHA256_ITERATIONS or _argon2_available()


def configure_local_auth(db: FinanceDB, username: str, password: str, role: str = "admin", enabled: bool = True) -> dict[str, Any]:
    db.init()
    payload = {
        "enabled": bool(enabled),
        "users": {
            username: {
                "role": role,
                "password_hash": hash_password(password).to_dict(),
            }
        },
        "note": "Local session auth with PBKDF2-HMAC-SHA256 password storage. Do not expose the web UI to the public internet.",
    }
    db.set_setting(SECURITY_SETTING, payload)
    users_dict: dict[str, Any] = payload["users"]  # type: ignore[assignment]
    return {"enabled": payload["enabled"], "users": list(users_dict.keys())}


def get_security_config(db: FinanceDB) -> dict[str, Any]:
    res: dict[str, Any] = db.get_setting(SECURITY_SETTING, {"enabled": False, "users": {}})
    return res


def is_loopback_bind_host(host: str) -> bool:
    normalized = (host or "").strip().lower()
    return normalized in LOCAL_BIND_HOSTS


def is_auth_enabled(db: FinanceDB) -> bool:
    config = get_security_config(db)
    users = config.get("users") if isinstance(config, dict) else None
    if not isinstance(users, dict) or config.get("enabled") is not True:
        return False
    for user in users.values():
        if not isinstance(user, dict) or not isinstance(user.get("password_hash"), dict):
            continue
        try:
            stored = PasswordHash.from_dict(user["password_hash"])
            if stored.algorithm == "argon2id" and stored.encoded.startswith("$argon2id$"):
                return True
            if (stored.algorithm == "pbkdf2_sha256" and
                    stored.iterations >= PBKDF2_SHA256_ITERATIONS and
                    len(base64.b64decode(stored.salt_b64, validate=True)) >= 16 and
                    len(base64.b64decode(stored.digest_b64, validate=True)) == 32):
                return True
        except (TypeError, ValueError, OverflowError):
            continue
    return False


def assert_safe_bind_host(db: FinanceDB, host: str, *, service: str) -> None:
    """Fail closed when private services are bound outside loopback without auth.

    First-run is stricter than the normal app because ordinary auth may not be
    configured yet. A non-loopback first-run requires FINANCE_AGENT_SETUP_TOKEN
    so the setup screen is not silently exposed to the LAN.
    """
    if is_loopback_bind_host(host):
        return
    if service == "FastAPI API":
        raise RuntimeError("FastAPI API has no remote authentication; bind to loopback")
    first_run_required = False
    if first_run_required and not os.environ.get("FINANCE_AGENT_SETUP_TOKEN"):
        raise RuntimeError(
            f"Refusing to start {service} on host {host!r} during first-run without FINANCE_AGENT_SETUP_TOKEN. "
            "Bind to 127.0.0.1 or set a one-time setup token explicitly."
        )
    if is_auth_enabled(db):
        return
    raise RuntimeError(
        f"Refusing to start {service} on host {host!r} without enabled authentication. "
        "Run `finance-agent --db <db> security-setup --username admin --password <strong-password>` "
        "or bind to 127.0.0.1."
    )


def verify_local_auth(db: FinanceDB, username: str, password: str) -> tuple[bool, str]:
    config = get_security_config(db)
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return False, ""
    users = config.get("users")
    if not isinstance(users, dict):
        return False, ""
    user = users.get(username)
    if not isinstance(user, dict):
        return False, ""
    try:
        stored = PasswordHash.from_dict(user.get("password_hash") or {})
        ok = verify_password(password, stored)
    except (TypeError, ValueError, OverflowError):
        return False, ""
    if ok and password_hash_needs_upgrade(stored):
        config.setdefault("users", {})[username]["password_hash"] = hash_password(password).to_dict()
        db.set_setting(SECURITY_SETTING, config)
    return ok, str(user.get("role") or "viewer") if ok else ""


def ensure_first_run_setup_token(db: FinanceDB) -> str:
    """Create and store a one-time first-run token hash; return the raw token.

    The raw value is only available to the current process and should be printed
    to the local console or injected into a loopback-only page. The DB stores a
    PBKDF2 hash so the token is not recoverable from the SQLite file.
    """
    token = os.environ.get("FINANCE_AGENT_SETUP_TOKEN") or secure_token(24)
    payload = db.get_setting(SETUP_TOKEN_SETTING, None)
    if not payload:
        db.set_setting(SETUP_TOKEN_SETTING, {"enabled": True, "password_hash": hash_password(token).to_dict(), "created_at": int(time.time())})
    return token


def verify_first_run_setup_token(db: FinanceDB, token: str) -> bool:
    payload = db.get_setting(SETUP_TOKEN_SETTING, None)
    if not payload or not payload.get("enabled"):
        return False
    stored = PasswordHash.from_dict(payload.get("password_hash") or {})
    return verify_password(token or "", stored)


def clear_first_run_setup_token(db: FinanceDB) -> None:
    db.set_setting(SETUP_TOKEN_SETTING, {"enabled": False, "used_at": int(time.time())})


def _get_or_create_secret(db: FinanceDB, key: str) -> bytes:
    value: str = db.get_setting(key, "")
    if not value:
        value = secure_token(32)
        db.set_setting(key, value)
    return value.encode("utf-8")


def issue_session_token(db: FinanceDB, username: str, role: str, *, ttl_seconds: int = 12 * 60 * 60) -> str:
    issued = int(time.time())
    expires = issued + max(60, int(ttl_seconds))
    nonce = secure_token(16)
    payload = f"{username}|{role}|{issued}|{expires}|{nonce}"
    secret = _get_or_create_secret(db, SESSION_SECRET_SETTING)
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload.encode("utf-8") + b"." + base64.urlsafe_b64encode(sig)).decode("ascii")


def verify_session_token(db: FinanceDB, token: str) -> tuple[bool, str, str]:
    try:
        token_bytes = token.encode("ascii")
        raw = base64.b64decode(token_bytes, altchars=b"-_", validate=True)
        # Reject non-canonical or padded tokens with trailing garbage. Python's
        # urlsafe_b64decode is intentionally permissive, so verify the exact
        # encoded form we issued before trusting the payload.
        if base64.urlsafe_b64encode(raw).decode("ascii") != token:
            return False, "", ""
        payload_raw, sig_b64 = raw.rsplit(b".", 1)
        payload = payload_raw.decode("utf-8")
        expected = hmac.new(_get_or_create_secret(db, SESSION_SECRET_SETTING), payload_raw, hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(sig_b64)
        if not hmac.compare_digest(actual, expected):
            return False, "", ""
        username, role, _issued, expires, _nonce = payload.split("|", 4)
        if int(expires) < int(time.time()):
            return False, "", ""
        return True, username, role
    except Exception:  # noqa: BLE001
        return False, "", ""


def _csrf_session_binding(session_token: str | None) -> str:
    # Never store the raw session in the CSRF token; bind to a stable digest.
    value = session_token or "preauth"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_csrf_token(db: FinanceDB, session_token: str | None = None) -> str:
    nonce = secure_token(16)
    session_binding = _csrf_session_binding(session_token)
    secret = _get_or_create_secret(db, CSRF_SECRET_SETTING)
    payload = f"{session_binding}.{nonce}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{session_binding}.{nonce}.{sig}"


def verify_csrf_token(db: FinanceDB, token: str, session_token: str | None = None) -> bool:
    try:
        session_binding, nonce, sig = (token or "").split(".", 2)
    except ValueError:
        return False
    expected_binding = _csrf_session_binding(session_token)
    if not hmac.compare_digest(session_binding, expected_binding):
        return False
    payload = f"{session_binding}.{nonce}"
    expected = hmac.new(_get_or_create_secret(db, CSRF_SECRET_SETTING), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


class SimpleRateLimiter:
    """Small in-process token-bucket limiter for local desktop services."""

    def __init__(self, *, max_requests: int = 120, window_seconds: int = 60) -> None:
        self.max_requests = int(max_requests)
        self.window_seconds = int(window_seconds)
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        hits = [ts for ts in self._hits.get(key, []) if ts >= cutoff]
        if len(hits) >= self.max_requests:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


def encrypt_secret(value: str, key: str | bytes) -> str:
    try:
        from cryptography.fernet import Fernet  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - optional dependency path
        raise RuntimeError("Install optional extra: pip install -e '.[security]' to use encryption") from exc
    key_bytes = key if isinstance(key, bytes) else key.encode("ascii")
    res: str = Fernet(key_bytes).encrypt(value.encode("utf-8")).decode("ascii")
    return res


def decrypt_secret(token: str, key: str | bytes) -> str:
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:  # pragma: no cover - optional dependency path
        raise RuntimeError("Install optional extra: pip install -e '.[security]' to use encryption") from exc
    key_bytes = key if isinstance(key, bytes) else key.encode("ascii")
    res: str = Fernet(key_bytes).decrypt(token.encode("ascii")).decode("utf-8")
    return res


def render_security_status(db: FinanceDB) -> str:
    config = get_security_config(db)
    users = config.get("users") or {}
    lines = ["Security status", f"auth_enabled: {bool(config.get('enabled'))}", f"users: {', '.join(users.keys()) or 'none'}"]
    preferred = "Argon2id" if _argon2_available() else f"PBKDF2-HMAC-SHA256 fallback, {PBKDF2_SHA256_ITERATIONS} iterations"
    lines.append(f"password_storage: {preferred}, per-user random salt")
    lines.append("web_auth: signed local session cookie + session-bound HMAC CSRF token + POST origin checks")
    lines.append("api_auth: HTTP Basic over local/private transport + role gate for unsafe methods")
    lines.append("rate_limit: in-process local rate limiter for web/API request bursts")
    lines.append("secret_encryption: optional Fernet via .[security]")
    lines.append("policy: no Sber login/password storage; no cookies/SMS/2FA bypass")
    return "\n".join(lines)
