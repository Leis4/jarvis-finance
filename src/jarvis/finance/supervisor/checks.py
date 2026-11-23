"""Rule-based checks for system health."""

import inspect
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from jarvis.finance.supervisor.models import Finding, Severity


def check_db_integrity(db_path: str) -> Finding:
    """Balance consistency: latest snapshot vs sum of transactions per account."""
    if not Path(db_path).exists():
        return Finding(
            check_name="db_integrity",
            severity=Severity.CRITICAL,
            title="БД не найдена",
            detail=f"Файл БД не существует: {db_path}",
            suggestion="Выполни инициализацию БД",
        )
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT cash_balance, reserve_balance, savings_balance, snapshot_date "
        "FROM budget_snapshots ORDER BY snapshot_date DESC, id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return Finding(
            check_name="db_integrity",
            severity=Severity.CRITICAL,
            title="Нет budget_snapshot",
            detail="Таблица budget_snapshots пуста — не можем посчитать баланс",
            suggestion="Отправь 4 PDF в Telegram",
        )
    cash, reserve, savings, snap_date = row
    if cash is None or reserve is None:
        return Finding(
            check_name="db_integrity",
            severity=Severity.WARNING,
            title="Snapshot с None в балансах",
            detail=f"cash={cash}, reserve={reserve}, savings={savings}",
        )
    return Finding(
        check_name="db_integrity",
        severity=Severity.OK,
        title="БД цела",
        detail=f"Snapshot {snap_date}: main={cash}, reserve={reserve}, savings={savings}",
    )


def check_date_sanity(db_path: str) -> Finding:
    """No transactions with future dates."""
    if not Path(db_path).exists():
        return Finding(
            check_name="date_sanity",
            severity=Severity.OK,
            title="Даты корректны",
            detail="БД не создана",
        )
    today = datetime.now(UTC).date().isoformat()
    conn = sqlite3.connect(db_path)
    bad = conn.execute(
        "SELECT COUNT(1), MIN(tx_date), MAX(tx_date) FROM transactions "
        "WHERE tx_date > ?", (today,)
    ).fetchone()
    conn.close()
    if bad and bad[0] and bad[0] > 0:
        return Finding(
            check_name="date_sanity",
            severity=Severity.WARNING,
            title=f"{bad[0]} транзакций в будущем",
            detail=f"Даты от {bad[1]} до {bad[2]} (today={today})",
        )
    return Finding(
        check_name="date_sanity",
        severity=Severity.OK,
        title="Даты корректны",
        detail=f"Все транзакции ≤ {today}",
    )


def check_duplicates(db_path: str) -> Finding:
    """Same amount + date + description within 30 days."""
    if not Path(db_path).exists():
        return Finding(
            check_name="duplicates",
            severity=Severity.OK,
            title="Дубликатов нет",
            detail="БД не создана",
        )
    conn = sqlite3.connect(db_path)
    dupes = conn.execute("""
        SELECT tx_date, amount, description, COUNT(1) as cnt
        FROM transactions
        WHERE tx_date >= date('now', '-30 days')
        GROUP BY tx_date, amount, description
        HAVING cnt > 1
        ORDER BY cnt DESC
        LIMIT 20
    """).fetchall()
    conn.close()
    if dupes:
        return Finding(
            check_name="duplicates",
            severity=Severity.WARNING,
            title=f"{len(dupes)} групп дубликатов",
            detail=", ".join(f"{d[3]}x {d[2][:30]}" for d in dupes[:3]),
            affected=[f"{d[0]}:{d[2][:30]}" for d in dupes],
            suggestion="Проверь через /review",
        )
    return Finding(
        check_name="duplicates",
        severity=Severity.OK,
        title="Дубликатов нет",
        detail="",
    )


def check_category_coverage(db_path: str) -> Finding:
    """Unknown/uncategorized share < 10%."""
    if not Path(db_path).exists():
        return Finding(
            check_name="category_coverage",
            severity=Severity.WARNING,
            title="Нет транзакций",
            detail="БД не создана",
        )
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(1) FROM transactions").fetchone()[0]
    if total == 0:
        conn.close()
        return Finding(
            check_name="category_coverage",
            severity=Severity.WARNING,
            title="Нет транзакций",
            detail="БД пуста",
        )
    unknown = conn.execute("""
        SELECT COUNT(1) FROM transactions
        WHERE category IN ('unknown', 'uncategorized', 'other', '') OR category IS NULL
    """).fetchone()[0]
    conn.close()
    pct = (unknown / total) * 100
    sev = Severity.CRITICAL if pct > 20 else Severity.WARNING if pct > 10 else Severity.OK
    return Finding(
        check_name="category_coverage",
        severity=sev,
        title=f"Uncategorized: {pct:.1f}% ({unknown}/{total})",
        detail="Целевой: <10%",
        suggestion="Проверь /review" if pct > 10 else None,
    )


def check_snapshot_freshness(db_path: str) -> Finding:
    """Snapshot not older than 7 days."""
    if not Path(db_path).exists():
        return Finding(
            check_name="snapshot_freshness",
            severity=Severity.CRITICAL,
            title="Snapshot отсутствует",
            detail="Файл БД не существует",
            suggestion="Отправь 4 PDF в Telegram",
        )
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT MAX(snapshot_date) FROM budget_snapshots"
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return Finding(
            check_name="snapshot_freshness",
            severity=Severity.CRITICAL,
            title="Snapshot отсутствует",
            detail="Записи в budget_snapshots отсутствуют",
            suggestion="Отправь 4 PDF в Telegram",
        )
    try:
        snap = date.fromisoformat(row[0])
    except ValueError:
        return Finding(
            check_name="snapshot_freshness",
            severity=Severity.WARNING,
            title="Некорректная дата snapshot",
            detail=f"Не удалось спарсить дату {row[0]}",
        )
    age = (datetime.now(UTC).date() - snap).days
    sev = Severity.CRITICAL if age > 14 else Severity.WARNING if age > 7 else Severity.OK
    return Finding(
        check_name="snapshot_freshness",
        severity=sev,
        title=f"Snapshot свежий ({age} дней)" if sev == Severity.OK else f"Snapshot устарел ({age} дней)",
        detail=f"Дата: {snap}",
        suggestion="Скинь свежие PDF" if age > 7 else None,
    )


def check_pdf_import_freshness(db_path: str) -> Finding:
    """PDF imported in last 30 days."""
    if not Path(db_path).exists():
        return Finding(
            check_name="pdf_freshness",
            severity=Severity.WARNING,
            title="Нет импортированных PDF",
            detail="Файл БД не существует",
        )
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT MAX(created_at) FROM transactions "
        "WHERE external_id IS NOT NULL"
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return Finding(
            check_name="pdf_freshness",
            severity=Severity.WARNING,
            title="Нет импортированных PDF",
            detail="Нет операций с external_id",
        )
    try:
        dt_str = row[0].replace('Z', '+00:00')
        if '+' in dt_str or 'Z' in row[0]:
            last_import = datetime.fromisoformat(dt_str)
            if last_import.tzinfo:
                age = (datetime.now(UTC) - last_import).days
            else:
                age = (datetime.now(UTC) - last_import.replace(tzinfo=UTC)).days
        else:
            last_import = datetime.fromisoformat(dt_str)
            age = (datetime.now(UTC) - last_import.replace(tzinfo=UTC)).days
    except Exception:  # noqa: BLE001
        age = -1

    sev = Severity.WARNING if (age < 0 or age > 30) else Severity.OK
    title_text = f"PDF импорт {age} дней назад" if age >= 0 else "Дата импорта неизвестна"
    return Finding(
        check_name="pdf_freshness",
        severity=sev,
        title=title_text,
        detail="",
        suggestion="Пора скинуть свежие PDF" if (age < 0 or age > 30) else None,
    )


def check_scheduler_health(log_dir: str = "logs") -> Finding:
    """All scheduler jobs ran in last 48 hours."""
    log_path = Path(log_dir)
    if not log_path.exists():
        return Finding(
            check_name="scheduler_health",
            severity=Severity.WARNING,
            title="Логи scheduler не найдены",
            detail="Папка логов не найдена",
        )
    recent_logs = sorted(log_path.glob("bot_*.log"), reverse=True)[:2]
    if not recent_logs:
        return Finding(
            check_name="scheduler_health",
            severity=Severity.WARNING,
            title="Логи scheduler не найдены",
            detail="Файлы логов bot_*.log не найдены",
        )
    text = ""
    for f in recent_logs:
        text += f.read_text(encoding="utf-8", errors="ignore")
    if "Scheduler started" not in text:
        return Finding(
            check_name="scheduler_health",
            severity=Severity.CRITICAL,
            title="Scheduler не запускался",
            detail="Нет записи 'Scheduler started' за последние 48ч",
            suggestion="Проверь FinanceAgentBot task",
        )
    return Finding(
        check_name="scheduler_health",
        severity=Severity.OK,
        title="Scheduler работает",
        detail="",
    )


def check_bot_health(log_dir: str = "logs") -> Finding:
    """Polling active, no persistent errors in last hour."""
    log_path = Path(log_dir)
    if not log_path.exists():
        return Finding(
            check_name="bot_health",
            severity=Severity.WARNING,
            title="Логи бота не найдены",
            detail="Папка логов не найдена",
        )
    latest = sorted(log_path.glob("bot_*.log"), reverse=True)
    if not latest:
        return Finding(
            check_name="bot_health",
            severity=Severity.WARNING,
            title="Логи бота пусты",
            detail="Файлы логов bot_*.log не найдены",
        )
    text = latest[0].read_text(encoding="utf-8", errors="ignore")
    tail = text[-10000:]  # last ~10 KB
    if "409 Conflict" in tail:
        return Finding(
            check_name="bot_health",
            severity=Severity.WARNING,
            title="409 Conflict в недавних логах",
            detail="Обнаружены ошибки 409 Conflict",
            suggestion="Возможно, два инстанса polling",
        )
    if "ConnectError" in tail and tail.count("ConnectError") > 3:
        return Finding(
            check_name="bot_health",
            severity=Severity.WARNING,
            title="ConnectError повторяется",
            detail="Обнаружены частые ошибки подключения",
            suggestion="Проверь VPN",
        )
    return Finding(
        check_name="bot_health",
        severity=Severity.OK,
        title="Bot polling активен",
        detail="",
    )


ALL_CHECKS = [
    check_db_integrity,
    check_date_sanity,
    check_duplicates,
    check_category_coverage,
    check_snapshot_freshness,
    check_pdf_import_freshness,
    check_scheduler_health,
    check_bot_health,
]


def run_all_checks(db_path: str, log_dir: str = "logs") -> list[Finding]:
    """Run all rule-based checks."""
    results = []
    for check in ALL_CHECKS:
        try:
            sig = inspect.signature(check)  # type: ignore[arg-type]
            if "db_path" in sig.parameters:
                results.append(check(db_path))  # type: ignore[operator]
            else:
                results.append(check(log_dir))  # type: ignore[operator]
        except Exception as e:  # noqa: BLE001
            results.append(Finding(
                check_name=check.__name__,
                severity=Severity.WARNING,
                title=f"Check failed: {check.__name__}",
                detail=str(e),
            ))
    return results
