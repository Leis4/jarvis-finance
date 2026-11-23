"""Statistical anomaly detection."""

import math
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import numpy as np  # type: ignore[import-not-found]
    HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore[assignment]
    HAS_NUMPY = False

from jarvis.finance.supervisor.models import Finding, Severity


def _load_daily_spending(db_path: str, days: int = 90) -> dict[str, float]:
    if not Path(db_path).exists():
        return {}
    cutoff = (datetime.now(UTC).date() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT tx_date, SUM(ABS(amount))
        FROM transactions
        WHERE amount < 0 AND tx_date >= ?
          AND category NOT IN ('transfer', 'internal_transfer', 'savings_transfer')
        GROUP BY tx_date ORDER BY tx_date
    """, (cutoff,)).fetchall()
    conn.close()
    return {r[0]: float(r[1]) for r in rows}


def detect_daily_anomalies(db_path: str, days: int = 90) -> Finding:
    """Isolation Forest / Z-score on daily spending. Flags unusual high-spend days."""
    daily = _load_daily_spending(db_path, days)
    if len(daily) < 14:
        return Finding(
            check_name="daily_anomalies",
            severity=Severity.INFO,
            title="Мало данных для anomaly detection",
            detail=f"{len(daily)} дней < 14 минимум",
        )

    dates = sorted(daily.keys())
    raw_vals = [daily[d] for d in dates]

    outliers: list[tuple[str, float, float]] = []

    if np is not None:
        values = np.array(raw_vals).reshape(-1, 1)
        try:
            from pyod.models.iforest import IForest  # type: ignore[import-not-found]
            model = IForest(contamination=0.05, random_state=42)
            model.fit(values)
            labels = model.labels_  # 1 = outlier
            scores = model.decision_function(values)
            outliers = [(dates[i], float(values[i][0]), float(scores[i]))
                        for i in range(len(dates)) if labels[i] == 1]
        except Exception:  # noqa: BLE001
            # Fallback: 2σ rule with numpy
            mean = float(values.mean())
            std = float(values.std())
            if std > 0:
                outliers = [(dates[i], float(values[i][0]), float((values[i][0] - mean) / std))
                            for i in range(len(dates))
                            if (values[i][0] - mean) / std > 2.0]
    else:
        # Pure python 2σ fallback
        mean = sum(raw_vals) / len(raw_vals)
        variance = sum((x - mean) ** 2 for x in raw_vals) / len(raw_vals)
        std = math.sqrt(variance)
        if std > 0:
            outliers = [(dates[i], raw_vals[i], (raw_vals[i] - mean) / std)
                        for i in range(len(dates))
                        if (raw_vals[i] - mean) / std > 2.0]

    if outliers:
        top = sorted(outliers, key=lambda x: -x[1])[:3]
        return Finding(
            check_name="daily_anomalies",
            severity=Severity.WARNING,
            title=f"{len(outliers)} аномальных дней",
            detail="; ".join(f"{d}: {a:.0f}₽" for d, a, _ in top),
            affected=[d for d, _, _ in outliers],
        )
    return Finding(
        check_name="daily_anomalies",
        severity=Severity.OK,
        title="Траты в норме",
        detail="",
    )


def detect_category_outliers(db_path: str, days: int = 30) -> Finding:
    """Z-score per category. Flags categories spending >2σ above their own mean."""
    if not Path(db_path).exists():
        return Finding(
            check_name="category_outliers",
            severity=Severity.OK,
            title="Категории в норме",
            detail="БД не создана",
        )
    today = datetime.now(UTC).date()
    cutoff_30 = (today - timedelta(days=30)).isoformat()
    cutoff_90 = (today - timedelta(days=90)).isoformat()

    conn = sqlite3.connect(db_path)
    # Last 30d per category
    recent = conn.execute("""
        SELECT category, SUM(ABS(amount))
        FROM transactions
        WHERE amount < 0 AND tx_date >= ?
          AND category NOT IN ('transfer', 'internal_transfer', 'savings_transfer')
        GROUP BY category
    """, (cutoff_30,)).fetchall()
    conn.close()

    conn = sqlite3.connect(db_path)
    baseline = conn.execute("""
        SELECT category, 
               strftime('%Y-%m', tx_date) as month,
               SUM(ABS(amount)) as total
        FROM transactions
        WHERE amount < 0 AND tx_date >= ?
          AND category NOT IN ('transfer', 'internal_transfer', 'savings_transfer')
        GROUP BY category, month
    """, (cutoff_90,)).fetchall()
    conn.close()

    monthly: dict[str, list[float]] = defaultdict(list)
    for cat, _, total in baseline:
        monthly[cat].append(float(total))

    outliers = []
    for cat, recent_total in recent:
        cat_str = str(cat)
        val = float(recent_total)
        if cat_str not in monthly or len(monthly[cat_str]) < 2:
            continue
        vals = monthly[cat_str]
        if np is not None:
            arr = np.array(vals)
            mean = float(arr.mean())
            std = float(arr.std())
        else:
            mean = sum(vals) / len(vals)
            variance = sum((x - mean) ** 2 for x in vals) / len(vals)
            std = math.sqrt(variance)

        if std < 1:
            continue
        z = (val - mean) / std
        if z > 2.0:
            outliers.append((cat_str, val, mean, z))

    if outliers:
        top = sorted(outliers, key=lambda x: -x[3])[:3]
        return Finding(
            check_name="category_outliers",
            severity=Severity.WARNING,
            title=f"{len(outliers)} категорий выше нормы",
            detail="; ".join(f"{c}: {r:.0f}₽ vs {m:.0f}₽ (z={z:.1f})" for c, r, m, z in top),
        )
    return Finding(
        check_name="category_outliers",
        severity=Severity.OK,
        title="Категории в норме",
        detail="",
    )


ALL_ANOMALY_CHECKS = [detect_daily_anomalies, detect_category_outliers]


def run_anomaly_checks(db_path: str) -> list[Finding]:
    return [check(db_path) for check in ALL_ANOMALY_CHECKS]
