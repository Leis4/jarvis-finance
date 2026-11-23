"""Owner devices, persistent sessions, pairing and WebAuthn passkeys.

Everything here is local: SQLite in the private review workspace, hashed bearer
secrets, and public keys only. The owner key remains the recovery path.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEVICE_TTL_DAYS = 90
PAIR_TTL_SECONDS = 60
CHALLENGE_TTL_SECONDS = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions(
    token_hash TEXT PRIMARY KEY,
    csrf TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    device_id TEXT
);
CREATE TABLE IF NOT EXISTS challenges(
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    challenge TEXT NOT NULL,
    device_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    opened INTEGER NOT NULL DEFAULT 0,
    approved INTEGER NOT NULL DEFAULT 0,
    approved_by TEXT,
    label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS credentials(
    id TEXT PRIMARY KEY,
    device_id TEXT,
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    transports TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT 'iPhone',
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS owner_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class OwnerAuthStore:
    """Private SQLite store for sessions, devices, pairings and passkeys."""

    def __init__(self, path: Path, *, public_origin: str = "") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.public_origin = public_origin.strip().rstrip("/")
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------- meta --------------------------------
    def owner_handle(self) -> bytes:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM owner_meta WHERE key='handle'").fetchone()
            if row:
                return _unb64(row["value"])
            handle = secrets.token_bytes(16)
            conn.execute("INSERT INTO owner_meta(key,value) VALUES('handle',?)", (_b64(handle),))
            return handle

    # ------------------------------ sessions -----------------------------
    def save_session(self, token: str, csrf: str, expires_at: datetime,
                     device_id: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO sessions(token_hash,csrf,created_at,expires_at,device_id)"
                         " VALUES(?,?,?,?,?)",
                         (hash_secret(token), csrf, utc_now().isoformat(),
                          expires_at.isoformat(), device_id))

    def load_session(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE token_hash=?",
                               (hash_secret(token),)).fetchone()
            if row is None:
                return None
            if parse_stamp(row["expires_at"]) <= utc_now():
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (row["token_hash"],))
                return None
            return dict(row)

    def delete_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (hash_secret(token),))

    def delete_device_sessions(self, device_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE device_id=?", (device_id,))

    # ------------------------------- devices -----------------------------
    def create_device(self, label: str, *, user_agent: str = "",
                      ttl_days: int = DEVICE_TTL_DAYS) -> tuple[dict[str, Any], str]:
        ident = secrets.token_hex(16)
        secret = new_secret()
        now = utc_now()
        expires = now + timedelta(days=ttl_days)
        clean = " ".join(str(label).split())[:60] or "Устройство"
        with self._connect() as conn:
            conn.execute("INSERT INTO devices(id,label,token_hash,created_at,last_seen_at,expires_at,user_agent)"
                         " VALUES(?,?,?,?,?,?,?)",
                         (ident, clean, hash_secret(secret), now.isoformat(), now.isoformat(),
                          expires.isoformat(), str(user_agent)[:200]))
        return {"id": ident, "label": clean, "created_at": now.isoformat(),
                "expires_at": expires.isoformat()}, f"{ident}.{secret}"

    def find_device(self, cookie_value: str) -> dict[str, Any] | None:
        if not cookie_value or "." not in cookie_value:
            return None
        ident, secret = cookie_value.split(".", 1)
        if not re.fullmatch(r"[0-9a-f]{32}", ident) or not secret:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id=?", (ident,)).fetchone()
            if row is None or row["revoked_at"]:
                return None
            if parse_stamp(row["expires_at"]) <= utc_now():
                return None
            if not secrets.compare_digest(row["token_hash"], hash_secret(secret)):
                return None
            conn.execute("UPDATE devices SET last_seen_at=? WHERE id=?",
                         (utc_now().isoformat(), ident))
            return dict(row)

    def list_devices(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id,label,created_at,last_seen_at,expires_at,revoked_at,user_agent"
                                " FROM devices ORDER BY created_at DESC").fetchall()
            creds = {r["device_id"]: r["n"] for r in conn.execute(
                "SELECT device_id, COUNT(*) AS n FROM credentials GROUP BY device_id")}
        now = utc_now()
        result = []
        for row in rows:
            item = dict(row)
            item["expired"] = parse_stamp(item["expires_at"]) <= now
            item["passkeys"] = creds.get(item["id"], 0)
            result.append(item)
        return result

    def revoke_device(self, ident: str) -> bool:
        with self._connect() as conn:
            changed = conn.execute("UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                   (utc_now().isoformat(), ident)).rowcount
            conn.execute("DELETE FROM sessions WHERE device_id=?", (ident,))
            conn.execute("DELETE FROM credentials WHERE device_id=?", (ident,))
            return bool(changed)

    def revoke_all(self) -> int:
        with self._connect() as conn:
            changed = conn.execute("UPDATE devices SET revoked_at=? WHERE revoked_at IS NULL",
                                   (utc_now().isoformat(),)).rowcount
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM credentials")
            return int(changed)

    # ----------------------------- challenges ----------------------------
    def new_challenge(self, kind: str, *, device_id: str | None = None,
                      ttl: int = CHALLENGE_TTL_SECONDS, label: str = "") -> dict[str, Any]:
        ident = secrets.token_hex(16)
        challenge = _b64(secrets.token_bytes(32))
        now = utc_now()
        with self._connect() as conn:
            conn.execute("INSERT INTO challenges(id,kind,challenge,device_id,created_at,expires_at,label)"
                         " VALUES(?,?,?,?,?,?,?)",
                         (ident, kind, challenge, device_id, now.isoformat(),
                          (now + timedelta(seconds=ttl)).isoformat(), label[:60]))
            conn.execute("DELETE FROM challenges WHERE expires_at < ?",
                         ((now - timedelta(days=1)).isoformat(),))
        return {"id": ident, "challenge": challenge,
                "expires_at": (now + timedelta(seconds=ttl)).isoformat()}

    def get_challenge(self, ident: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM challenges WHERE id=?", (ident,)).fetchone()
        return dict(row) if row else None

    def mark_opened(self, ident: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE challenges SET opened=1 WHERE id=? AND used=0", (ident,))

    def approve(self, ident: str, approved_by: str) -> bool:
        with self._connect() as conn:
            return bool(conn.execute(
                "UPDATE challenges SET approved=1, approved_by=? WHERE id=? AND used=0"
                " AND expires_at > ?", (approved_by, ident, utc_now().isoformat())).rowcount)

    def consume(self, ident: str, kind: str) -> dict[str, Any] | None:
        """One-time consumption; expired or already-used rows return None."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM challenges WHERE id=?", (ident,)).fetchone()
            if row is None or row["kind"] != kind or row["used"]:
                return None
            if parse_stamp(row["expires_at"]) <= utc_now():
                return None
            conn.execute("UPDATE challenges SET used=1 WHERE id=?", (ident,))
        return dict(row)

    # ---------------------------- credentials ----------------------------
    def add_credential(self, credential_id: str, public_key: bytes, *, device_id: str | None,
                       sign_count: int = 0, transports: str = "", label: str = "iPhone") -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO credentials"
                         "(id,device_id,public_key,sign_count,transports,label,created_at,last_used_at)"
                         " VALUES(?,?,?,?,?,?,?,?)",
                         (credential_id, device_id, public_key, int(sign_count), transports,
                          label[:60], utc_now().isoformat(), utc_now().isoformat()))

    def list_credentials(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id,device_id,label,created_at,last_used_at,sign_count,transports"
                                " FROM credentials ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def credential(self, credential_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM credentials WHERE id=?", (credential_id,)).fetchone()
        return dict(row) if row else None

    def touch_credential(self, credential_id: str, sign_count: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE credentials SET sign_count=?, last_used_at=? WHERE id=?",
                         (int(sign_count), utc_now().isoformat(), credential_id))

    def delete_credential(self, credential_id: str) -> bool:
        with self._connect() as conn:
            return bool(conn.execute("DELETE FROM credentials WHERE id=?", (credential_id,)).rowcount)

    def attach_credential(self, credential_id: str, device_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE credentials SET device_id=? WHERE id=?", (device_id, credential_id))


# ------------------------------- webauthn --------------------------------
RP_NAME = "JARVIS"


def rp_id_for(public_origin: str) -> str:
    """Exact RP ID: the configured public host, else loopback for local use."""
    from urllib.parse import urlsplit

    host = urlsplit(public_origin).hostname if public_origin else ""
    if host and "." in host:
        return host
    return "localhost"


def allowed_origins(public_origin: str) -> list[str]:
    origins = ["http://127.0.0.1:8766", "http://localhost:8766"]
    if public_origin:
        origins.insert(0, public_origin.rstrip("/"))
    return origins


def registration_options(store: OwnerAuthStore, *, rp_id: str, label: str) -> tuple[dict[str, Any], str]:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    challenge = store.new_challenge("register", label=label)
    exclude = [PublicKeyCredentialDescriptor(id=_unb64(cred["id"]))
               for cred in store.list_credentials()]
    options = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_name="owner",
        user_id=store.owner_handle(), user_display_name="Владелец JARVIS",
        challenge=_unb64(challenge["challenge"]), exclude_credentials=exclude or None,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED,
            resident_key=ResidentKeyRequirement.PREFERRED))
    return json.loads(options_to_json(options)), challenge["id"]


def verify_registration(store: OwnerAuthStore, challenge_id: str, credential: Any, *,
                        rp_id: str, origins: list[str], device_id: str | None,
                        label: str) -> dict[str, Any]:
    from webauthn import verify_registration_response

    stored = store.get_challenge(challenge_id)
    if stored is None or stored["kind"] != "register":
        raise ValueError("registration_challenge_invalid")
    verified = verify_registration_response(
        credential=credential, expected_challenge=_unb64(stored["challenge"]),
        expected_rp_id=rp_id, expected_origin=origins, require_user_verification=True)
    credential_id = _b64(verified.credential_id)
    transports = ""
    try:
        transports = ",".join(credential.get("response", {}).get("transports") or [])
    except AttributeError:
        transports = ""
    store.consume(challenge_id, "register")
    store.add_credential(credential_id, verified.credential_public_key,
                         device_id=device_id, sign_count=verified.sign_count,
                         transports=transports, label=label)
    return {"credential_id": credential_id, "device_id": device_id,
            "sign_count": verified.sign_count}


def authentication_options(store: OwnerAuthStore, *, rp_id: str) -> tuple[dict[str, Any], str]:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )

    challenge = store.new_challenge("authenticate")
    allow = [PublicKeyCredentialDescriptor(id=_unb64(cred["id"])) for cred in store.list_credentials()]
    options = generate_authentication_options(
        rp_id=rp_id, challenge=_unb64(challenge["challenge"]), allow_credentials=allow or None,
        user_verification=UserVerificationRequirement.REQUIRED)
    return json.loads(options_to_json(options)), challenge["id"]


def verify_authentication(store: OwnerAuthStore, challenge_id: str, credential: Any, *,
                          rp_id: str, origins: list[str]) -> dict[str, Any]:
    from webauthn import verify_authentication_response

    stored = store.get_challenge(challenge_id)
    if stored is None or stored["kind"] != "authenticate":
        raise ValueError("authentication_challenge_invalid")
    credential_id = str(credential.get("id") or "")
    if not credential_id:
        raise ValueError("credential_missing")
    row = store.credential(credential_id)
    if row is None:
        raise ValueError("credential_unknown")
    verified = verify_authentication_response(
        credential=credential, expected_challenge=_unb64(stored["challenge"]),
        expected_rp_id=rp_id, expected_origin=origins,
        credential_public_key=row["public_key"],
        credential_current_sign_count=int(row["sign_count"]),
        require_user_verification=True)
    store.touch_credential(credential_id, verified.new_sign_count)
    store.consume(challenge_id, "authenticate")
    return {"credential_id": credential_id, "device_id": row["device_id"],
            "sign_count": verified.new_sign_count}
