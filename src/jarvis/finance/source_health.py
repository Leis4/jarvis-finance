"""Persist source availability separately from successful zero-valued results."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.readiness import saved_source_entry


def record_source_state(db: FinanceDB, source: str, status: str, coverage_end: str | None = None, error_type: str | None = None) -> None:
    """Keep last success when recording a later failure."""
    previous = db.get_setting(f"source_health:{source}", {})
    now = datetime.now(UTC).isoformat()
    db.set_setting(f"source_health:{source}", {
        "status": status, "checked_at": now, "error_type": error_type,
        "last_success_at": now if status == "ok" else previous.get("last_success_at"),
        "coverage_end": coverage_end if status == "ok" else previous.get("coverage_end"),
    })


def source_health(db: FinanceDB, sources: list[str], now: datetime | None = None) -> dict[str, Any]:
    """Return observed freshness without assuming missing sources are current."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    result: dict[str, Any] = {}
    for source in sources:
        state = db.get_setting(f"source_health:{source}", None)
        availability = saved_source_entry(state, current)
        observed = state.get("status") if isinstance(state, dict) else None
        legacy = {"status": observed if isinstance(observed, str)
                  and observed in {"ok", "error", "unavailable", "blocked", "stale"} else "unavailable",
                  "checked_at": availability["last_attempt"], "last_success_at": availability["last_success"],
                  "coverage_end": (availability["coverage"] or {}).get("end"),
                  "error_type": availability["source_error"]}
        result[source] = {**legacy, "age_seconds": availability["age_seconds"],
                          "availability_status": availability["status"], "availability": availability}
    return result
