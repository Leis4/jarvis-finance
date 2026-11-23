from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DBEncryptionStatus:
    configured: bool
    driver_available: bool
    driver_name: str
    encrypted_when_opened: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def connect_sqlite(path: str | Path, **kwargs: Any) -> sqlite3.Connection:
    db_key = os.environ.get("FINANCE_AGENT_DB_KEY")
    if not db_key:
        return sqlite3.connect(path, **kwargs)  # type: ignore[no-any-return]

    driver = _load_sqlcipher_driver()
    conn = driver.connect(str(path), **kwargs)
    escaped = db_key.replace("'", "''")
    conn.execute(f"PRAGMA key = '{escaped}'")
    conn.execute("PRAGMA cipher_integrity_check")
    return conn  # type: ignore[no-any-return]


def _load_sqlcipher_driver() -> Any:
    try:
        import pysqlcipher3.dbapi2 as sqlcipher  # type: ignore

        return sqlcipher
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        import sqlcipher3 as sqlcipher  # type: ignore

        return sqlcipher
    except Exception as exc:
        raise RuntimeError(
            "FINANCE_AGENT_DB_KEY is set, but no SQLCipher driver is installed. "
            "Install pysqlcipher3/sqlcipher3 or unset FINANCE_AGENT_DB_KEY for unencrypted local SQLite."
        ) from exc


def detect_sqlcipher_driver() -> tuple[bool, str]:
    try:
        _load_sqlcipher_driver()
        try:
            import pysqlcipher3.dbapi2  # noqa: F401

            return True, "pysqlcipher3"
        except Exception:  # noqa: BLE001
            return True, "sqlcipher3"
    except Exception:  # noqa: BLE001
        return False, ""


def encryption_status() -> DBEncryptionStatus:
    configured = bool(os.environ.get("FINANCE_AGENT_DB_KEY"))
    driver_available, driver_name = detect_sqlcipher_driver()
    if configured and driver_available:
        return DBEncryptionStatus(True, True, driver_name, True, "FINANCE_AGENT_DB_KEY is set and a SQLCipher-compatible driver is available.")
    if configured and not driver_available:
        return DBEncryptionStatus(True, False, "", False, "FINANCE_AGENT_DB_KEY is set, but no SQLCipher-compatible driver is installed. The app fails closed.")
    if driver_available:
        return DBEncryptionStatus(False, True, driver_name, False, "SQLCipher driver is available, but FINANCE_AGENT_DB_KEY is not set; DB opens as normal SQLite.")
    return DBEncryptionStatus(False, False, "", False, "No DB encryption key is configured; local SQLite is unencrypted by default.")


def render_encryption_status(status: DBEncryptionStatus) -> str:
    return "\n".join(
        [
            "Database encryption status",
            f"configured: {status.configured}",
            f"driver_available: {status.driver_available}",
            f"driver_name: {status.driver_name or 'none'}",
            f"encrypted_when_opened: {status.encrypted_when_opened}",
            f"message: {status.message}",
        ]
    )
