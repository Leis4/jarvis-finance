from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.finance.db import FinanceDB


@dataclass(frozen=True)
class AnalyticsExport:
    engine: str
    output_path: Path
    rows: int
    message: str


def category_summary(db: FinanceDB) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT category,
                   COUNT(*) AS tx_count,
                   ROUND(SUM(CASE WHEN CAST(amount AS REAL) < 0 THEN ABS(CAST(amount AS REAL)) ELSE 0 END), 2) AS spent,
                   ROUND(SUM(CASE WHEN CAST(amount AS REAL) > 0 THEN CAST(amount AS REAL) ELSE 0 END), 2) AS income
            FROM transactions
            GROUP BY category
            ORDER BY spent DESC, income DESC, category ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def export_category_summary(db: FinanceDB, output_path: str | Path) -> AnalyticsExport:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = category_summary(db)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return AnalyticsExport("sqlite-json", path, len(rows), "Category summary exported as JSON.")
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["category", "tx_count", "spent", "income"])
        writer.writeheader()
        writer.writerows(rows)
    return AnalyticsExport("sqlite-csv", path, len(rows), "Category summary exported as CSV.")


def try_duckdb_category_summary(db: FinanceDB) -> tuple[str, list[dict[str, Any]]]:
    """Use DuckDB when available, otherwise fall back to SQLite.

    DuckDB is optional because the local MVP must work without heavy data extras.
    """
    try:
        import duckdb  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return "sqlite-fallback", category_summary(db)

    con = duckdb.connect(database=":memory:")
    try:
        sqlite_path = str(db.path)
        con.execute("INSTALL sqlite;")
        con.execute("LOAD sqlite;")
        con.execute("CALL sqlite_attach(?)", [sqlite_path])
        result = con.execute(
            """
            SELECT category,
                   COUNT(*) AS tx_count,
                   ROUND(SUM(CASE WHEN CAST(amount AS DOUBLE) < 0 THEN ABS(CAST(amount AS DOUBLE)) ELSE 0 END), 2) AS spent,
                   ROUND(SUM(CASE WHEN CAST(amount AS DOUBLE) > 0 THEN CAST(amount AS DOUBLE) ELSE 0 END), 2) AS income
            FROM transactions
            GROUP BY category
            ORDER BY spent DESC, income DESC, category ASC
            """
        ).fetchall()
        columns = [str(c[0]) for c in con.description]
        return "duckdb", [dict(zip(columns, row)) for row in result]
    except Exception:  # noqa: BLE001
        return "sqlite-fallback", category_summary(db)
    finally:
        con.close()


def render_category_summary(rows: list[dict[str, Any]], engine: str = "sqlite") -> str:
    if not rows:
        return f"Category summary ({engine}): no transactions."
    lines = [f"Category summary ({engine}):"]
    for row in rows:
        lines.append(f"- {row['category']}: spent={row['spent']} income={row['income']} tx={row['tx_count']}")
    return "\n".join(lines)
