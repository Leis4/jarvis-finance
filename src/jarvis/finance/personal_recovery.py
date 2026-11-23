"""Local backup verification and read-only CFO readiness evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from jarvis.finance.db import FinanceDB


def file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def logical_sha(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for line in conn.iterdump():
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def verify_backup(path: Path) -> dict[str, Any]:
    """Restore into a disposable database; never replace the owner's database."""
    receipt = json.loads(path.with_suffix(".receipt.json").read_text(encoding="utf-8"))
    if receipt.get("version") != 1 or receipt.get("sha256") != file_sha(path):
        raise ValueError("Backup receipt mismatch")
    with (TemporaryDirectory(prefix="jarvis-restore-") as directory,
          closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source,
          closing(sqlite3.connect(Path(directory) / "restore.sqlite")) as restored):
        source.backup(restored)
        if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Restored backup integrity failed")
        if logical_sha(restored) != receipt.get("logical_sha256"):
            raise ValueError("Restored backup content mismatch")
    return {"status": "verified", "sha256": receipt["sha256"], "created_at": receipt["created_at"]}


def create_backup(db: FinanceDB, directory: Path) -> Path:
    """Back up SQLite's committed view and exercise restoration before acceptance."""
    if not db.path.is_file():
        raise ValueError("Cannot back up a missing source database")
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    target = directory / (now.strftime("cfo-%Y%m%dT%H%M%S%fZ") + ".sqlite")
    # Exclusive creation also prevents overwriting any existing backup.
    with target.open("xb"):
        pass
    with db.connect() as source, closing(sqlite3.connect(target)) as dest:
        source.backup(dest)
        content_sha = logical_sha(dest)
    receipt = {"version": 1, "created_at": now.isoformat(), "sha256": file_sha(target),
        "logical_sha256": content_sha}
    with target.with_suffix(".receipt.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True)
    verify_backup(target)
    return target


def health(db: FinanceDB, *, now: datetime | None = None) -> dict[str, Any]:
    """Report local readiness without network, credentials or financial payloads."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Aware health timestamp required")
    if not db.path.is_file():
        return {"status": "unavailable", "reason": "database_missing"}
    try:
        with db.connect() as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return {"status": "unavailable", "reason": "database_integrity"}
        capture = db.get_setting("cfo.last_capture", None)
        snapshot = db.get_setting("cfo.bank_snapshot", {})
        if not capture or not snapshot:
            return {"status": "degraded", "reason": "bank_not_observed"}
        capture_sha = hashlib.sha256(json.dumps(capture, sort_keys=True).encode()).hexdigest()
        if capture_sha != snapshot.get("capture_sha256"):
            return {"status": "degraded", "reason": "capture_snapshot_mismatch"}
        age = now - datetime.fromisoformat(snapshot["observed_at"])
        fresh = timedelta(0) <= age <= timedelta(minutes=15)
        attempt = db.get_setting("cfo.last_refresh", {})
        failed = attempt.get("status") not in {None, "updated"}
        return {"status": "ready" if fresh and not failed else "degraded",
            "reason": "bank_refresh_failed" if failed else "fresh" if fresh else "bank_stale",
            "observed_at": snapshot["observed_at"], "financial_history_complete": False,
            "held_operations": snapshot.get("held_operations"), "network_probe": False}
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return {"status": "unavailable", "reason": "local_evidence_invalid"}
