from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from jarvis.finance.db import FinanceDB
from jarvis.finance.migrations import current_schema_version, migration_status


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    message: str


REQUIRED_TABLES = {
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
}

OPTIONAL_PACKAGES = {
    "torch": "PyTorch ML forecasts",
    "fastapi": "JSON API",
    "uvicorn": "API server runtime",
    "pydantic": "strict validation schemas",
    "polars": "fast tabular analytics",
    "apscheduler": "live scheduler runner",
    "typer": "modern typed CLI",
    "sqlmodel": "future ORM/API models",
    "alembic": "industrial database migrations",
    "ruff": "lint and format checks",
}


def _package_installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def run_doctor(db: FinanceDB) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    db.init()

    checks.append(DoctorCheck("database_path", db.path.exists(), str(db.path)))

    with db.connect() as conn:
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = sorted(REQUIRED_TABLES - tables)
        checks.append(
            DoctorCheck(
                "required_tables",
                not missing,
                "all required tables exist" if not missing else "missing: " + ", ".join(missing),
            )
        )
        try:
            conn.execute("PRAGMA integrity_check").fetchone()
            checks.append(DoctorCheck("sqlite_integrity", True, "PRAGMA integrity_check completed"))
        except sqlite3.DatabaseError as exc:
            checks.append(DoctorCheck("sqlite_integrity", False, str(exc)))

    statuses = migration_status(db)
    pending = [status.version for status in statuses if not status.applied]
    checks.append(
        DoctorCheck(
            "migrations",
            not pending,
            f"schema={current_schema_version(db)}" if not pending else "pending: " + ", ".join(pending),
        )
    )

    rules = db.get_setting("rules", None)
    checks.append(DoctorCheck("rules", rules is not None, "rules are configured" if rules is not None else "rules missing; run init"))

    latest_snapshot = db.latest_budget_snapshot()
    checks.append(
        DoctorCheck(
            "budget_snapshot",
            latest_snapshot is not None,
            "latest snapshot exists" if latest_snapshot else "no factual cash/reserve/savings snapshot yet",
        )
    )

    for package, purpose in OPTIONAL_PACKAGES.items():
        installed = _package_installed(package)
        checks.append(
            DoctorCheck(
                f"optional_{package}",
                True,
                f"installed for {purpose}" if installed else f"not installed; optional for {purpose}",
            )
        )
    return checks


def render_doctor(checks: Iterable[DoctorCheck]) -> str:
    lines = ["Finance Agent doctor:"]
    hard_fail = False
    for check in checks:
        status = "OK" if check.ok else "WARN"
        if not check.ok and not check.name.startswith("optional_"):
            hard_fail = True
        lines.append(f"- {status} {check.name}: {check.message}")
    lines.append("result: " + ("needs attention" if hard_fail else "ready"))
    return "\n".join(lines)
