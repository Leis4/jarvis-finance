"""Notification triggers - detect conditions in DB."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from jarvis.finance.notifications.models import EventType, NotificationEvent, Severity

logger = logging.getLogger(__name__)


def check_forecast_negative(db_path: str) -> NotificationEvent | None:
    """Forecast shows negative balance in 14 days."""
    try:
        from jarvis.finance.db import FinanceDB
        from jarvis.finance.forecast.forecaster import compute_forecast

        db = FinanceDB(db_path)
        report = compute_forecast(db, horizon_days=14)
        if report.status != "experimental" or report.projected_balance_in_14d is None:
            logger.warning("forecast unavailable: %s", report.status)
            return None
        if report.projected_balance_in_14d >= 0:
            return None
        return NotificationEvent(
            event_type=EventType.FORECAST_NEGATIVE,
            severity=Severity.WARNING,
            title="📉 Research-прогноз: минус",
            message=(
                "Экспериментальный baseline; обязательства отдельно\n"
                f"Остаток через 14 дней: {report.projected_balance_in_14d:.0f}₽\n"
                f"Метод: {report.used_model}"
            ),
            data={"horizon_days": 14, "status": "experimental"},
            dedup_key=f"forecast_neg_{datetime.now(UTC).date().isoformat()}",
        )
    except Exception:
        logger.exception("forecast check failed")
        return None


def check_low_balance(db_path: str, threshold: float = 1000.0) -> NotificationEvent | None:
    """Current balance below threshold."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("""
            SELECT cash_balance FROM budget_snapshots
            ORDER BY snapshot_date DESC LIMIT 1
        """).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if not row:
        return None
    balance = float(row[0] or 0)
    if balance >= threshold:
        return None
    return NotificationEvent(
        event_type=EventType.LOW_BALANCE,
        severity=Severity.WARNING if balance > 500 else Severity.CRITICAL,
        title="💰 Низкий баланс",
        message=f"Баланс: {balance:.0f}₽ (ниже {threshold:.0f}₽)",
        data={"balance": balance},
        dedup_key=f"low_balance_{datetime.now(UTC).date().isoformat()}",
    )


def check_large_expense(db_path: str, threshold: float = 3000.0) -> NotificationEvent | None:
    """Large expense in last 24h."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("""
            SELECT amount, description, tx_date FROM transactions
            WHERE amount < ? AND tx_date >= date('now', '-1 day')
            ORDER BY ABS(amount) DESC LIMIT 1
        """, (-threshold,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if not row:
        return None
    amount, desc, tx_date = row
    return NotificationEvent(
        event_type=EventType.LARGE_EXPENSE,
        severity=Severity.WARNING,
        title="💸 Крупная трата",
        message=f"{abs(float(amount)):.0f}₽ — {(desc or '')[:60]}\nДата: {tx_date}",
        data={"amount": abs(float(amount))},
        dedup_key=f"large_{tx_date}_{abs(float(amount)):.0f}",
    )


def check_new_subscription(db_path: str) -> NotificationEvent | None:
    """New recurring subscription in last 7 days."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("""
            SELECT description, COUNT(1) as cnt, MIN(tx_date)
            FROM transactions
            WHERE category = 'subscriptions' AND amount < 0
              AND tx_date >= date('now', '-7 days')
            GROUP BY description
            HAVING cnt >= 2
            LIMIT 1
        """).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if not row:
        return None
    desc, cnt, first_date = row
    return NotificationEvent(
        event_type=EventType.NEW_SUBSCRIPTION,
        severity=Severity.INFO,
        title="📱 Возможно новая подписка",
        message=f"«{(desc or '')[:60]}» — {cnt} списаний за 7 дней",
        data={"description": desc},
        dedup_key=f"new_sub_{(desc or '')[:40]}_{first_date}",
    )


def check_anomaly(db_path: str) -> NotificationEvent | None:
    """Supervisor found anomaly."""
    try:
        from jarvis.finance.supervisor.anomaly import detect_daily_anomalies

        finding = detect_daily_anomalies(db_path, days=30)
        if finding.severity.value == "OK":
            return None
        return NotificationEvent(
            event_type=EventType.ANOMALY,
            severity=Severity.WARNING,
            title="⚠️ Аномалия",
            message=f"{finding.title}\n{finding.detail}",
            data={"detail": finding.detail},
            dedup_key=f"anomaly_{datetime.now(UTC).date().isoformat()}",
        )
    except Exception:
        logger.exception("anomaly check failed")
        return None


def check_new_compensation(db_path: str) -> NotificationEvent | None:
    """New compensation (incoming from friend) in last 24h."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("""
            SELECT t.amount, t.description, t.tx_date
            FROM incoming_classifications ic
            JOIN transactions t ON ic.income_tx_id = t.id
            WHERE ic.transfer_type = 'compensation'
              AND t.tx_date >= date('now', '-1 day')
            ORDER BY t.tx_date DESC LIMIT 1
        """).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if not row:
        return None
    amount, desc, tx_date = row
    return NotificationEvent(
        event_type=EventType.NEW_COMPENSATION,
        severity=Severity.INFO,
        title="🤝 Новая компенсация",
        message=f"{float(amount):.0f}₽ — {(desc or '')[:60]}",
        data={"amount": float(amount)},
        dedup_key=f"comp_{tx_date}_{float(amount):.0f}",
    )


ALL_CHECKS = [
    check_forecast_negative,
    check_low_balance,
    check_large_expense,
    check_new_subscription,
    check_anomaly,
    check_new_compensation,
]


def run_all_checks(db_path: str) -> list[NotificationEvent]:
    """Run all trigger checks and return list of active events."""
    events: list[NotificationEvent] = []
    for check in ALL_CHECKS:
        try:
            e = check(db_path)
            if e:
                events.append(e)
        except Exception:
            logger.exception("%s failed", check.__name__)
    return events
