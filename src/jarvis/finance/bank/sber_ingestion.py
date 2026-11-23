from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.bank.sber_statement import read_sber_export
from jarvis.finance.db import FinanceDB


@dataclass(frozen=True)
class SberPreview:
    file_path: str
    file_hash: str
    account: str
    source_format: str
    parsed_count: int
    duplicate_file: bool
    total_income: Decimal
    total_expense: Decimal
    warnings: list[str]
    preview_rows: list[dict[str, Any]]


@dataclass(frozen=True)
class SberImportCommit:
    file_path: str
    file_hash: str
    parsed_count: int
    inserted_count: int
    skipped_duplicate_file: bool
    warnings: list[str]


def file_sha256(path: str | Path) -> str:
    source = Path(path)
    digest = hashlib.sha256()
    with source.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_sber_manifest_tables(db: FinanceDB) -> None:
    db.init()
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sber_import_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT 'sber',
                source_format TEXT NOT NULL DEFAULT '',
                parsed_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                warnings_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                UNIQUE(file_hash, account)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sber_import_files_hash ON sber_import_files(file_hash)")


def was_file_imported(db: FinanceDB, file_hash: str, account: str = "sber") -> bool:
    ensure_sber_manifest_tables(db)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM sber_import_files WHERE file_hash = ? AND account = ?",
            (file_hash, account),
        ).fetchone()
    return row is not None


def build_sber_preview(db: FinanceDB, file_path: str | Path, account: str = "sber", limit: int = 10) -> SberPreview:
    source = Path(file_path)
    digest = file_sha256(source)
    result = read_sber_export(source, account=account)
    total_income = Decimal("0.00")
    total_expense = Decimal("0.00")
    preview_rows: list[dict[str, Any]] = []
    for tx in result.transactions:
        if tx.amount >= 0:
            total_income += tx.amount
        else:
            total_expense += abs(tx.amount)
        if len(preview_rows) < limit:
            preview_rows.append(
                {
                    "date": tx.tx_date.isoformat(),
                    "amount": str(tx.amount),
                    "category": tx.category,
                    "merchant": tx.merchant,
                    "description": tx.description,
                    "external_id": tx.external_id,
                }
            )
    return SberPreview(
        file_path=str(source),
        file_hash=digest,
        account=account,
        source_format=result.source_format,
        parsed_count=len(result.transactions),
        duplicate_file=was_file_imported(db, digest, account=account),
        total_income=total_income.quantize(Decimal("0.01")),
        total_expense=total_expense.quantize(Decimal("0.01")),
        warnings=result.warnings,
        preview_rows=preview_rows,
    )


def commit_sber_import(db: FinanceDB, file_path: str | Path, account: str = "sber", force: bool = False) -> SberImportCommit:
    source = Path(file_path)
    digest = file_sha256(source)
    if was_file_imported(db, digest, account=account) and not force:
        return SberImportCommit(str(source), digest, 0, 0, True, ["file hash was already imported for this account"])
    result = read_sber_export(source, account=account)
    inserted = db.add_transactions(result.transactions, source="sber_statement", parser_version="1")
    ensure_sber_manifest_tables(db)
    with db.connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO sber_import_files(file_path, file_hash, account, source_format, parsed_count, inserted_count, warnings_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    str(source),
                    digest,
                    account,
                    result.source_format,
                    len(result.transactions),
                    inserted,
                    json.dumps(result.warnings, ensure_ascii=False),
                ),
            )
            db.audit(conn, "sber_file_import_recorded", {"file_hash": digest[:12], "inserted": inserted})
        except Exception:
            if not force:
                raise
    return SberImportCommit(str(source), digest, len(result.transactions), inserted, False, result.warnings)


def render_sber_preview(preview: SberPreview) -> str:
    lines = [
        "Sber import preview",
        f"file: {preview.file_path}",
        f"sha256: {preview.file_hash}",
        f"account: {preview.account}",
        f"format: {preview.source_format}",
        f"parsed: {preview.parsed_count}",
        f"duplicate_file: {preview.duplicate_file}",
        f"income: {preview.total_income} RUB",
        f"expenses: {preview.total_expense} RUB",
        "",
        "Rows:",
    ]
    for row in preview.preview_rows:
        lines.append(f"- {row['date']} | {row['amount']} | {row['category']} | {row['merchant']} | {row['description']}")
    if preview.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in preview.warnings[:20]:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def render_sber_commit(commit: SberImportCommit) -> str:
    lines = [
        "Sber import commit",
        f"file: {commit.file_path}",
        f"sha256: {commit.file_hash}",
        f"parsed: {commit.parsed_count}",
        f"inserted: {commit.inserted_count}",
        f"skipped_duplicate_file: {commit.skipped_duplicate_file}",
    ]
    if commit.warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in commit.warnings[:20])
    return "\n".join(lines)
