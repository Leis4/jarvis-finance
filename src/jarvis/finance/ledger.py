"""Source-event journal and exact account reconciliation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from jarvis.finance.domain import Transaction
from jarvis.finance.event_taxonomy import (
    EXPENSE_CATEGORIES,
    NON_ECONOMIC_CATEGORIES,
    TAXONOMY_VERSION,
    UNRESOLVED_CATEGORIES,
    canonical_category,
)

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_events (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    account TEXT NOT NULL,
    event_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    original_json TEXT NOT NULL,
    transaction_id INTEGER REFERENCES transactions(id),
    status TEXT NOT NULL CHECK(status IN ('accepted', 'pending_review')),
    imported_at TEXT NOT NULL,
    UNIQUE(source, account, event_key, fingerprint)
);
CREATE TABLE IF NOT EXISTS category_corrections (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    version INTEGER NOT NULL,
    old_category TEXT NOT NULL,
    new_category TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(transaction_id, version)
);
CREATE TABLE IF NOT EXISTS account_balance_observations (
    id INTEGER PRIMARY KEY,
    account TEXT NOT NULL,
    currency TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    amount TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account, currency, asof_date, source)
);
CREATE TABLE IF NOT EXISTS transaction_allocations (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    position INTEGER NOT NULL,
    category TEXT NOT NULL,
    amount TEXT NOT NULL,
    PRIMARY KEY(transaction_id, position)
);
"""

def finite_money(value: object) -> Decimal:
    """Reject missing and non-finite monetary inputs rather than inventing zero."""
    try:
        amount = Decimal(str(value))
    except ArithmeticError as exc:
        raise ValueError("Invalid monetary input") from exc
    if not amount.is_finite():
        raise ValueError("Monetary input must be finite")
    return amount


def direction_conflict(amount: Decimal, category: str) -> bool:
    """An expense label on a credit proves neither income nor a refund."""
    return amount > 0 and canonical_category(category) in EXPENSE_CATEGORIES


def conflicting_identities(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """A reused account/native ID with different money requires source review."""
    identities: dict[tuple[str, str], set[tuple[str, Decimal, str]]] = {}
    for row in rows:
        if row.get('account') and row.get('external_id'):
            key = str(row['account']), str(row['external_id'])
            identities.setdefault(key, set()).add((str(row.get('tx_date', '')),
                finite_money(row['amount']), str(row.get('currency', ''))))
    return {key for key, facts in identities.items() if len(facts) > 1}


def cashflow(rows: list[dict[str, Any]], currency: str = "RUB") -> dict[str, Any]:
    """Aggregate economic flow while retaining signed account movement."""
    income = Decimal(0)
    expenses = Decimal(0)
    movement = Decimal(0)
    categories: dict[str, Decimal] = {}
    excluded = Decimal(0)
    unresolved = Decimal(0)
    unresolved_outflow = Decimal(0)
    ids: list[int] = []
    conflict_ids: set[int] = set()
    conflict_count = 0
    identities = conflicting_identities(rows)
    identity_ids: set[int] = set()
    identity_count = 0
    for row in rows:
        if str(row.get("currency", "RUB")) != currency:
            continue
        amount = finite_money(row["amount"])
        movement += amount
        if "id" in row:
            ids.append(int(row["id"]))
        parts = row.get("allocations") or [{"category": row.get("category", "uncategorized"), "amount": amount}]
        if sum((finite_money(p["amount"]) for p in parts), Decimal(0)) != amount:
            raise ValueError("Allocation sum differs from transaction amount")
        if row.get('identity_conflict') or (row.get('account') and row.get('external_id')
                and (str(row['account']), str(row['external_id'])) in identities):
            unresolved += amount
            unresolved_outflow += max(-amount, Decimal(0))
            identity_count += 1
            if 'id' in row:
                identity_ids.add(int(row['id']))
            continue
        for part in parts:
            value = finite_money(part["amount"])
            category = canonical_category(str(part["category"]))
            if category in UNRESOLVED_CATEGORIES:
                unresolved += value
                unresolved_outflow += max(-value, Decimal(0))
            elif category in NON_ECONOMIC_CATEGORIES:
                excluded += value
            elif category == "refund" or category.startswith("refund:"):
                target = category.split(":", 1)[1] if ":" in category else "refund"
                expenses -= value
                categories[target] = categories.get(target, Decimal(0)) - value
            elif direction_conflict(value, category):
                unresolved += value
                conflict_count += 1
                if 'id' in row:
                    conflict_ids.add(int(row['id']))
            elif value > 0:
                income += value
            else:
                expenses -= value
                categories[category] = categories.get(category, Decimal(0)) - value
    return {"income": income, "expenses": expenses, "net": income - expenses,
            "movement": movement, "excluded_movement": excluded,
            "unresolved_movement": unresolved,
            "unresolved_outflow": unresolved_outflow,
            "by_category": categories, "transaction_ids": ids, "currency": currency,
            "taxonomy_version": TAXONOMY_VERSION, "direction_conflict_ids": sorted(conflict_ids),
            "direction_conflict_count": conflict_count,
            "identity_conflict_ids": sorted(identity_ids), "identity_conflict_count": identity_count}


def transaction_payload(tx: Transaction) -> dict[str, str | None]:
    """Preserve parser output separately from subsequent user corrections."""
    return {
        "tx_date": tx.tx_date.isoformat(), "amount": str(tx.amount),
        "currency": tx.currency, "account": tx.account,
        "description": tx.description, "merchant": tx.merchant,
        "category": tx.category, "external_id": tx.external_id,
    }


def fingerprint(payload: dict[str, Any]) -> str:
    """Hash stable financial fields, excluding mutable classification."""
    material = {key: str(payload[key]) for key in (
        "tx_date", "amount", "currency", "account", "description", "merchant",
    )}
    material["amount"] = str(Decimal(material["amount"]).normalize())
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def record_source_event(
    conn: sqlite3.Connection, tx: Transaction, tx_id: int | None,
    source: str, parser_version: str, status: str,
) -> None:
    """Record one immutable observation, including conflicting reimports."""
    payload = transaction_payload(tx)
    key = tx.external_id or (f"manual:{tx_id}" if source == "manual" else fingerprint(payload))
    conn.execute(
        """INSERT OR IGNORE INTO source_events
        (source, parser_version, account, event_key, fingerprint, original_json,
         transaction_id, status, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, parser_version, tx.account, key, fingerprint(payload),
         json.dumps(payload, ensure_ascii=False), tx_id, status, datetime.now(UTC).isoformat()),
    )


@dataclass(frozen=True)
class Reconciliation:
    account: str
    currency: str
    start: date
    end: date
    opening: Decimal | None
    closing: Decimal | None
    movement: Decimal
    discrepancy: Decimal | None
    status: str
    transaction_ids: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {key: str(value) if isinstance(value, (date, Decimal)) else value for key, value in asdict(self).items()}


def reconcile(
    rows: list[dict[str, Any]], account: str, currency: str,
    start: date, end: date, opening: Decimal | None, closing: Decimal | None,
) -> Reconciliation:
    """Opening before start plus inclusive period movement must equal closing."""
    if end < start:
        raise ValueError("end precedes start")
    selected = [row for row in rows if row["account"] == account and row["currency"] == currency
                and start.isoformat() <= str(row["tx_date"]) <= end.isoformat()]
    movement = sum((finite_money(row["amount"]) for row in selected), Decimal(0))
    opening = None if opening is None else finite_money(opening)
    closing = None if closing is None else finite_money(closing)
    discrepancy = None if opening is None or closing is None else closing - opening - movement
    status = "unavailable" if discrepancy is None else "reconciled" if discrepancy == 0 else "mismatch"
    return Reconciliation(account, currency, start, end, opening, closing, movement,
                          discrepancy, status, tuple(int(row["id"]) for row in selected))
