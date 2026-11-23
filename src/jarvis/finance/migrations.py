from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jarvis.finance.db import FinanceDB, utc_now


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    sql: str = ""


@dataclass(frozen=True)
class MigrationStatus:
    version: str
    description: str
    applied: bool
    applied_at: str | None = None


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version="0001_v05_baseline",
        description="Mark the v0.5/v0.8 SQLite schema baseline created by FinanceDB.init().",
        sql="",
    ),
    Migration(
        version="0002_v06_agent_metadata",
        description="Add lightweight local metadata for product-readiness checks and future schema upgrades.",
        sql="""
        CREATE TABLE IF NOT EXISTS agent_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_transactions_category_date ON transactions(category, tx_date);
        CREATE INDEX IF NOT EXISTS idx_transactions_merchant_date ON transactions(merchant, tx_date);
        """,
    ),

    Migration(
        version="0003_v07_goals_guardrails",
        description="Add savings goals, goal contributions, and daily spending guardrails.",
        sql="""
        CREATE TABLE IF NOT EXISTS savings_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            target_amount TEXT NOT NULL,
            current_amount TEXT NOT NULL DEFAULT '0',
            priority INTEGER NOT NULL DEFAULT 3,
            deadline TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS goal_contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL,
            amount TEXT NOT NULL,
            contribution_date TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(goal_id) REFERENCES savings_goals(id)
        );
        CREATE TABLE IF NOT EXISTS budget_guardrails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guardrail_date TEXT NOT NULL,
            days_to_income INTEGER NOT NULL,
            free_cash TEXT NOT NULL,
            daily_safe_spend TEXT NOT NULL,
            recommended_freeze_categories TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_savings_goals_status_priority ON savings_goals(status, priority);
        CREATE INDEX IF NOT EXISTS idx_goal_contributions_goal ON goal_contributions(goal_id);
        CREATE INDEX IF NOT EXISTS idx_budget_guardrails_date ON budget_guardrails(guardrail_date);
        """,
    ),

    Migration(
        version="0004_v09_sber_ai_math",
        description="Add Sber import manifest and AI recommendation tables for v0.9 ingestion and math advisor.",
        sql="""
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
        );
        CREATE TABLE IF NOT EXISTS ai_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_type TEXT NOT NULL,
            title TEXT NOT NULL,
            formula TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            confidence TEXT NOT NULL DEFAULT '0',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sber_import_files_hash ON sber_import_files(file_hash);
        CREATE INDEX IF NOT EXISTS idx_ai_recommendations_type ON ai_recommendations(recommendation_type, status);
        """,
    ),
)


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def migration_status(db: FinanceDB) -> list[MigrationStatus]:
    db.init()
    with db.connect() as conn:
        _ensure_migration_table(conn)
        rows = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
        applied = {str(row["version"]): str(row["applied_at"]) for row in rows}
    return [
        MigrationStatus(
            version=migration.version,
            description=migration.description,
            applied=migration.version in applied,
            applied_at=applied.get(migration.version),
        )
        for migration in MIGRATIONS
    ]


def apply_migrations(db: FinanceDB) -> list[MigrationStatus]:
    """Apply known local migrations.

    This is intentionally stdlib-only. The project can later adopt Alembic for a
    richer migration workflow, but this minimal runner keeps the current local
    MVP installable without mandatory ORM dependencies.
    """
    db.init()
    applied_now: list[MigrationStatus] = []
    with db.connect() as conn:
        _ensure_migration_table(conn)
        existing = {
            str(row["version"])
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for migration in MIGRATIONS:
            if migration.version in existing:
                continue
            if migration.sql.strip():
                conn.executescript(migration.sql)
            applied_at = utc_now()
            conn.execute(
                "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.description, applied_at),
            )
            conn.execute(
                "INSERT OR REPLACE INTO agent_metadata(key, value, updated_at) VALUES (?, ?, ?)",
                ("schema_version", migration.version, applied_at),
            )
            db.audit(conn, "migration_applied", {"version": migration.version})
            applied_now.append(MigrationStatus(migration.version, migration.description, True, applied_at))
    return applied_now


def current_schema_version(db: FinanceDB) -> str:
    statuses = migration_status(db)
    applied = [status.version for status in statuses if status.applied]
    return applied[-1] if applied else "unmigrated"


def render_migration_status(statuses: list[MigrationStatus]) -> str:
    if not statuses:
        return "No migrations configured."
    lines = ["Schema migrations:"]
    for status in statuses:
        mark = "OK" if status.applied else "PENDING"
        suffix = f" at {status.applied_at}" if status.applied_at else ""
        lines.append(f"- {mark} {status.version}: {status.description}{suffix}")
    return "\n".join(lines)


def render_applied_migrations(statuses: list[MigrationStatus]) -> str:
    if not statuses:
        return "No new migrations were required."
    lines = ["Applied migrations:"]
    for status in statuses:
        lines.append(f"- {status.version}: {status.description}")
    return "\n".join(lines)
