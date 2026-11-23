"""Read-only statement checks and unresolved legacy ID review queues."""

from __future__ import annotations

import hashlib
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.bank.sber_statement import read_sber_export
from jarvis.finance.encrypted_sqlite import connect_sqlite
from jarvis.finance.ledger import fingerprint, reconcile, transaction_payload

REQUIRED_INTAKE_FIELDS = (
    "account", "currency", "start", "end", "opening", "closing",
    "operation_count", "movement", "source_reference",
)


def validate_statement_intake(statement: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Validate an original statement and its declared scope without reading a DB."""
    issues: list[str] = []
    if not statement.is_file():
        issues.append("statement_file_missing")
    elif statement.stat().st_size == 0:
        issues.append("statement_file_empty")
    suffix = statement.suffix.lower()
    if suffix not in {".csv", ".txt", ".tsv", ".xlsx", ".pdf"}:
        issues.append("statement_format_unsupported")
    missing = [field for field in REQUIRED_INTAKE_FIELDS if field not in metadata]
    issues.extend(f"metadata_missing_{field}" for field in missing)
    account = str(metadata.get("account", "")).strip()
    currency = str(metadata.get("currency", "")).strip().upper()
    if not account:
        issues.append("account_mapping_empty")
    if len(currency) != 3 or not currency.isalpha():
        issues.append("currency_invalid")
    if metadata.get("mapping_confirmed") is not True:
        issues.append("account_mapping_unconfirmed")
    if metadata.get("currency_evidence_confirmed") is not True:
        issues.append("currency_evidence_missing")
    try:
        start, end = date.fromisoformat(str(metadata.get("start", ""))), date.fromisoformat(str(metadata.get("end", "")))
        if end < start:
            issues.append("period_end_before_start")
    except ValueError:
        issues.append("period_invalid")
    for field in ("opening", "closing", "movement"):
        value = metadata.get(field)
        if value is None:
            issues.append(f"{field}_missing")
            continue
        try:
            if not Decimal(str(value)).is_finite():
                issues.append(f"{field}_not_finite")
        except (ArithmeticError, TypeError, ValueError):
            issues.append(f"{field}_invalid")
    count = metadata.get("operation_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        issues.append("operation_count_invalid")
    if not str(metadata.get("source_reference", "")).strip():
        issues.append("source_reference_missing")
    return {"valid": not issues, "issues": issues, "required_fields": list(REQUIRED_INTAKE_FIELDS),
            "source_format": suffix.lstrip(".") or "unknown", "database_modified": False}


def _rows(database: Path) -> list[dict[str, Any]]:
    if not database.is_file():
        raise FileNotFoundError(database)
    with closing(connect_sqlite(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        cursor = conn.execute("SELECT * FROM transactions ORDER BY id")
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def conflict_queue(database: Path) -> dict[str, Any]:
    """Return references and differences without choosing authoritative records."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in _rows(database):
        if row["external_id"]:
            groups.setdefault((row["account"], row["external_id"]), []).append(row)
    queue = []
    fields = ("tx_date", "amount", "currency", "description", "merchant")
    for key, records in sorted(groups.items()):
        if len(records) < 2:
            continue
        differing = [field for field in fields if len({str(row[field]) for row in records}) > 1]
        queue.append({
            "reference": hashlib.sha256(repr(key).encode()).hexdigest(),
            "transaction_ids": [row["id"] for row in records],
            "differing_fields": differing, "decision": "manual_review",
            "required_evidence": "original statement, account mapping and native event identifiers",
        })
    return {"status": "BLOCKED" if queue else "no_duplicate_ids", "groups": queue, "group_count": len(queue)}


def statement_dry_run(database: Path, statement: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Check declared statement scope without importing or changing balances."""
    intake = validate_statement_intake(statement, metadata)
    account, currency = str(metadata["account"]).strip(), str(metadata["currency"]).strip().upper()
    if not account or not currency or metadata.get("mapping_confirmed") is not True:
        raise ValueError("Explicit confirmed account/currency mapping is required")
    start, end = date.fromisoformat(metadata["start"]), date.fromisoformat(metadata["end"])
    if end < start:
        raise ValueError("end precedes start")
    existing = _rows(database)
    parsed = read_sber_export(statement, account=account)
    rows = [dict(transaction_payload(tx), id=index) for index, tx in enumerate(parsed.transactions, 1)]
    issues: list[str] = []
    if not any(row["account"] == account and row["currency"] == currency for row in existing):
        issues.append("account_currency_not_known_in_ledger")
    if parsed.warnings:
        issues.append("parser_warnings_require_review")
    if any(row["currency"] != currency for row in rows):
        issues.append("currency_mismatch")
    if any(not start.isoformat() <= str(row["tx_date"]) <= end.isoformat() for row in rows):
        issues.append("operation_outside_declared_period")
    # Parsers can derive IDs and default currencies: declaration is not bank evidence.
    if metadata.get("currency_evidence_confirmed") is not True:
        issues.append("currency_evidence_missing")
    opening = None if metadata.get("opening") is None else Decimal(str(metadata["opening"]))
    closing_balance = None if metadata.get("closing") is None else Decimal(str(metadata["closing"]))
    if any(value is not None and not value.is_finite() for value in (opening, closing_balance)):
        raise ValueError("Balances must be finite")
    totals = reconcile(rows, account, currency, start, end, opening, closing_balance).to_dict()
    if totals["status"] != "reconciled":
        issues.append("statement_balance_" + str(totals["status"]))
    movement = sum((Decimal(str(row["amount"])) for row in rows), Decimal(0))
    if metadata.get("operation_count") != len(rows):
        issues.append("operation_count_missing_or_mismatch")
    declared = metadata.get("movement")
    if declared is None or Decimal(str(declared)) != movement:
        issues.append("operation_sum_missing_or_mismatch")
    coverage = metadata.get("coverage_confirmed") is True and bool(metadata.get("source_reference"))
    if not coverage:
        issues.append("source_coverage_unconfirmed")
    reviews = []
    for row in rows:
        candidates = [old for old in existing if old["account"] == account and old["external_id"] == row["external_id"]]
        repeated = sum(other["external_id"] == row["external_id"] for other in rows) > 1
        if not repeated and len(candidates) == 1 and fingerprint(candidates[0]) == fingerprint(row):
            continue
        reviews.append({"statement_row": row["id"], "ledger_ids": [old["id"] for old in candidates],
                        "reason": "repeated_statement_id" if repeated else "conflicting_id" if candidates else "unknown_or_derived_id"})
    if reviews:
        issues.append("identifiers_require_manual_review")
    all_issues = list(dict.fromkeys(intake["issues"] + issues))
    return {"status": "BLOCKED" if all_issues else "checks_passed_dry_run_only",
            "statement_sha256": hashlib.sha256(statement.read_bytes()).hexdigest(),
            "account": account, "currency": currency, "period": [start.isoformat(), end.isoformat()],
            "reconciliation": totals, "movement_all_rows": str(movement), "parsed_count": len(rows),
            "warning_count": len(parsed.warnings), "coverage_confirmed": coverage,
            "issues": all_issues, "discrepancies": all_issues, "manual_review": reviews,
            "input_validation": intake, "database_modified": False}
