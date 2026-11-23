from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.finance.encrypted_sqlite import connect_sqlite

EXPORT_TABLES = [
    "transactions",
    "settings",
    "audit_log",
    "action_requests",
    "budget_snapshots",
    "category_spending_entries",
    "food_items",
    "food_price_observations",
    "scheduled_tasks",
    "scheduled_task_runs",
    "source_events",
    "category_corrections",
    "transaction_allocations",
    "account_balance_observations",
]


@dataclass(frozen=True)
class BackupResult:
    path: Path
    files: list[str]
    tables: list[str]
    created_at: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _available_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _table_rows(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[dict[str, Any]]]:
    cursor = conn.execute(f"SELECT * FROM {table}")
    columns = [desc[0] for desc in cursor.description or []]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return columns, rows


def export_table_csv(conn: sqlite3.Connection, table: str, path: Path) -> int:
    columns, rows = _table_rows(conn, table)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def export_table_json(conn: sqlite3.Connection, table: str, path: Path) -> int:
    _columns, rows = _table_rows(conn, table)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


def create_backup(db_path: str | Path, output_dir: str | Path = "./backups") -> BackupResult:
    """Create a self-contained local backup zip.

    The backup contains a raw SQLite copy plus CSV and JSON exports for inspectability.
    It never uploads data anywhere.
    """
    db_path = Path(db_path)
    output_dir = Path(output_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    work_dir = output_dir / f"finance-agent-backup-{stamp}"
    zip_path = output_dir / f"finance-agent-backup-{stamp}.zip"
    work_dir.mkdir(parents=True, exist_ok=False)

    files: list[str] = []
    copied_db = work_dir / db_path.name
    source_conn = connect_sqlite(db_path)
    snapshot_conn = connect_sqlite(copied_db)
    try:
        source_conn.backup(snapshot_conn)
    finally:
        snapshot_conn.close()
        source_conn.close()
    files.append(copied_db.relative_to(work_dir).as_posix())

    manifest: dict[str, Any] = {
        "created_at": utc_now(),
        "source_db": str(db_path),
        "database_file": copied_db.name,
        "database_sha256": hashlib.sha256(copied_db.read_bytes()).hexdigest(),
        "tables": {},
    }

    conn = connect_sqlite(copied_db)
    try:
        available = _available_tables(conn)
        for table in EXPORT_TABLES:
            if table not in available:
                continue
            csv_path = work_dir / "csv" / f"{table}.csv"
            json_path = work_dir / "json" / f"{table}.json"
            row_count = export_table_csv(conn, table, csv_path)
            export_table_json(conn, table, json_path)
            manifest["tables"][table] = {"rows": row_count}
            files.extend([csv_path.relative_to(work_dir).as_posix(), json_path.relative_to(work_dir).as_posix()])
    finally:
        conn.close()

    manifest_path = work_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    files.append("manifest.json")

    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(work_dir.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(work_dir).as_posix())

    shutil.rmtree(work_dir)
    return BackupResult(path=zip_path, files=sorted(files), tables=sorted(manifest["tables"].keys()), created_at=str(manifest["created_at"]))


def restore_backup(archive: str | Path, destination: str | Path) -> Path:
    """Validate a backup and restore to a new path, never overwriting a database."""
    target = Path(destination)
    if target.exists():
        raise FileExistsError("Restore destination already exists")
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        filename = manifest.get("database_file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("Backup manifest has no safe database_file")
        raw = zf.read(filename)
        if hashlib.sha256(raw).hexdigest() != manifest.get("database_sha256"):
            raise ValueError("Backup database checksum mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jarvis-restore-", dir=target.parent) as tmp:
        candidate = Path(tmp) / "verified.db"
        candidate.write_bytes(raw)
        conn = connect_sqlite(candidate)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored database integrity check failed")
            tables = _available_tables(conn)
            if "transactions" not in tables:
                raise ValueError("Backup does not contain finance transactions")
        finally:
            conn.close()
        with target.open("xb") as fh:
            fh.write(raw)
    return target
