"""Exact end-of-day statement balances with explicit coverage and freshness."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from jarvis.finance.ledger import finite_money


def account_timeline(rows: list[dict[str, Any]], *, account: str, currency: str,
                     start: date, end: date, opening: object, closing: object,
                     source_sha256: str, complete: bool, asof: date,
                     max_age_days: int = 1) -> dict[str, Any]:
    """Do not infer intraday ordering, missing balances, or complete account inventory."""
    if end < start or max_age_days < 0 or asof < end:
        raise ValueError("Invalid period or freshness policy")
    if len(source_sha256) != 64 or any(c not in "0123456789abcdef" for c in source_sha256):
        raise ValueError("Verified source hash required")
    before = None if opening is None else finite_money(opening)
    after = None if closing is None else finite_money(closing)
    days: dict[str, Decimal] = {}
    identities: set[str] = set()
    count = 0
    for row in rows:
        if row.get("account") != account or row.get("currency") != currency:
            raise ValueError("Statement contains a different account or currency")
        day = date.fromisoformat(str(row["tx_date"]))
        if not start <= day <= end:
            raise ValueError("Operation falls outside statement period")
        if row.get("id") is not None:
            ident = str(row["id"])
            if ident in identities:
                raise ValueError("Duplicate record identity; reconciliation required")
            identities.add(ident)
        days[day.isoformat()] = days.get(day.isoformat(), Decimal(0)) + finite_money(row["amount"])
        count += 1
    movement = sum(days.values(), Decimal(0))
    discrepancy = None if before is None or after is None else after - before - movement
    status = ("stale" if (asof - end).days > max_age_days else
              "insufficient_data" if not complete or discrepancy is None else
              "unavailable" if discrepancy != 0 else "available")
    running = before if status == "available" else None
    timeline = []
    for day_key, value in sorted(days.items()):
        if running is not None:
            running += value
        timeline.append({"date": day_key, "movement": str(value), "balance": None if running is None else str(running)})
    return {"status": status, "currency": currency, "period": [start.isoformat(), end.isoformat()],
            "source_sha256": source_sha256, "coverage_complete": complete, "asof": asof.isoformat(),
            "freshness_policy_days": max_age_days, "operation_count": count,
            "opening_observation": None if before is None else str(before),
            "closing_observation": None if after is None else str(after),
            "observed_movement": str(movement), "discrepancy": None if discrepancy is None else str(discrepancy),
            "verified_closing": str(after) if status == "available" else None, "end_of_day": timeline,
            "formula": "opening before period + inclusive signed movements = closing; daily aggregation",
            "database_modified": False, "economic_classification": "unverified"}
