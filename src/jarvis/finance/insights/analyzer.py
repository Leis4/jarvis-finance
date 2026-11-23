"""Detect spending trends and insights."""
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from jarvis.finance.insights.models import Insight, InsightType

TRANSFERS = ('transfer', 'internal_transfer', 'savings_transfer')


def _clean_merchant(desc: str) -> str:
    """Remove Sber boilerplate from merchant name."""
    if not desc:
        return ""
    desc = re.sub(
        r"\.?\s*Операция по (карте|счету|счёту)\s+\*+\d+.*$",
        "",
        desc,
        flags=re.IGNORECASE,
    )
    while True:
        new_desc = re.sub(
            r"\s+(MOSCOW|SANKT-PETERBU|SAINT-PETERSBURG|RUS|RU)\s*$",
            "",
            desc,
            flags=re.IGNORECASE,
        )
        if new_desc == desc:
            break
        desc = new_desc
    desc = re.sub(r"\.?\s*Операци.*$", "", desc, flags=re.IGNORECASE)
    return desc.strip()[:60]


def _category_by_week(db_path: str, weeks: int = 8) -> dict[str, list[float]]:
    """category -> [weekly_totals for last N weeks]."""
    today = datetime.now(UTC).date()
    result: dict[str, list[float]] = defaultdict(lambda: [0.0] * weeks)

    conn = sqlite3.connect(db_path)
    for w in range(weeks):
        start = today - timedelta(days=(weeks - w) * 7)
        end = today - timedelta(days=(weeks - w - 1) * 7)
        rows = conn.execute("""
            SELECT category, SUM(ABS(amount))
            FROM transactions
            WHERE amount < 0 AND tx_date >= ? AND tx_date < ?
              AND category NOT IN (?, ?, ?)
            GROUP BY category
        """, (start.isoformat(), end.isoformat(), *TRANSFERS)).fetchall()
        for cat, total in rows:
            result[cat][w] = float(total or 0)
    conn.close()
    return dict(result)


def _detect_trends(db_path: str, min_change_pct: float = 20.0) -> list[Insight]:
    """Find categories that trended up/down over last 4 weeks vs prior 4."""
    weekly = _category_by_week(db_path, weeks=8)
    insights = []

    for cat, weekly_totals in weekly.items():
        prior_4 = sum(weekly_totals[:4]) / 4
        last_4 = sum(weekly_totals[4:]) / 4
        if prior_4 < 100:  # skip if baseline too small unless new active category
            if last_4 > 500:
                insights.append(Insight(
                    type=InsightType.TREND_UP,
                    category=cat,
                    current=last_4, previous=0, change_pct=100,
                    message=f"{cat}: новая активная категория ({last_4:.0f}₽/нед)",
                    suggestion=f"Проверь новые траты в {cat}",
                    priority=4,
                ))
            continue
        change_pct = ((last_4 - prior_4) / prior_4) * 100
        if abs(change_pct) < min_change_pct:
            continue
        if change_pct > 0:
            insights.append(Insight(
                type=InsightType.TREND_UP,
                category=cat,
                current=last_4,
                previous=prior_4,
                change_pct=change_pct,
                message=f"{cat}: +{change_pct:.0f}% ({last_4:.0f}₽/нед vs {prior_4:.0f}₽/нед)",
                suggestion=f"Проверь, что изменилось в {cat}",
                priority=3 if change_pct > 60 else 5,
            ))
        else:
            insights.append(Insight(
                type=InsightType.TREND_DOWN,
                category=cat,
                current=last_4,
                previous=prior_4,
                change_pct=change_pct,
                message=f"{cat}: {change_pct:.0f}% ({last_4:.0f}₽/нед vs {prior_4:.0f}₽/нед)",
                suggestion=None,
                priority=7,
            ))
    return insights


def _detect_subscription_waste(db_path: str, idle_days: int = 60) -> list[Insight]:
    """Find subscription-category merchants not used recently."""
    conn = sqlite3.connect(db_path)
    cutoff = (datetime.now(UTC).date() - timedelta(days=idle_days)).isoformat()

    subs = conn.execute("""
        SELECT description, MAX(tx_date) as last_used, COUNT(*) as total,
               SUM(ABS(amount)) as total_spent
        FROM transactions
        WHERE category = 'subscriptions'
        GROUP BY description
    """).fetchall()
    conn.close()

    insights = []
    for desc, last_used, count, spent in subs:
        if last_used < cutoff and count > 1:  # had >1 charge, now idle
            clean_desc = _clean_merchant(desc)
            spent_f = float(spent or 0)
            monthly = spent_f / max(count, 1)
            insights.append(Insight(
                type=InsightType.UNUSED_SUB,
                category="subscriptions",
                current=spent_f,
                previous=0,
                change_pct=0,
                message=f"Подписка «{clean_desc}» не использовалась {idle_days}+ дней ({count} списаний, {spent_f:.0f}₽ всего)",
                suggestion=f"Отменить → экономия ~{monthly:.0f}₽/мес",
                priority=4,
                events_count=count,
                monthly_cost=monthly,
            ))
    insights.sort(key=lambda i: -i.monthly_cost)
    return insights


def _detect_category_anomalies(db_path: str) -> list[Insight]:
    """Categories 2σ above their own 90d baseline in last 30d."""
    from jarvis.finance.supervisor.anomaly import detect_category_outliers
    finding = detect_category_outliers(db_path)
    insights = []
    if finding.severity.value != "OK":
        insights.append(Insight(
            type=InsightType.SPIKE,
            category="multiple",
            current=0, previous=0, change_pct=0,
            message=finding.title,
            suggestion=finding.detail,
            priority=5,
        ))
    return insights


ALL_ANALYZERS = [_detect_trends, _detect_subscription_waste, _detect_category_anomalies]


def analyze(db_path: str, period_days: int = 30) -> list[Insight]:
    all_insights = []
    for fn in ALL_ANALYZERS:
        try:
            all_insights.extend(fn(db_path))
        except Exception:
            logging.getLogger(__name__).exception(f"{fn.__name__} failed")

    all_insights.sort(key=lambda i: (i.priority, -i.monthly_cost))
    return all_insights
