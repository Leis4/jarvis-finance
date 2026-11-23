"""Financial context builder for LLM prompts."""
from datetime import UTC, datetime, timedelta
from typing import Any

from jarvis.finance.control import TRANSFER_CATEGORIES
from jarvis.finance.db import FinanceDB


def build_financial_context(db: FinanceDB) -> str:
    """Build context string from DB for LLM."""
    snapshot = db.latest_budget_snapshot()
    bal = float(snapshot.get("cash_balance", 0)) if snapshot else 0.0
    res = float(snapshot.get("reserve_balance", 0)) if snapshot else 0.0
    sav = float(snapshot.get("savings_balance", 0)) if snapshot else 0.0

    today = datetime.now(UTC).date()
    cutoff_7d = (today - timedelta(days=7)).isoformat()
    cutoff_30d = (today - timedelta(days=30)).isoformat()

    txs = sorted(db.all_transactions(), key=lambda t: t["tx_date"], reverse=True)

    # Filter real expenses (no transfers)
    def is_expense(t: dict[str, Any]) -> bool:
        return (float(t["amount"]) < 0
                and t["category"] not in TRANSFER_CATEGORIES)

    # 7-day breakdown
    week_txs = [t for t in txs if is_expense(t) and t["tx_date"] >= cutoff_7d]
    week_total = sum(abs(float(t["amount"])) for t in week_txs)
    week_by_cat: dict[str, float] = {}
    for t in week_txs:
        c = str(t["category"])
        week_by_cat[c] = week_by_cat.get(c, 0.0) + abs(float(t["amount"]))

    # 30-day breakdown
    month_txs = [t for t in txs if is_expense(t) and t["tx_date"] >= cutoff_30d]
    month_total = sum(abs(float(t["amount"])) for t in month_txs)
    month_by_cat: dict[str, float] = {}
    for t in month_txs:
        c = str(t["category"])
        month_by_cat[c] = month_by_cat.get(c, 0.0) + abs(float(t["amount"]))

    # Latest 15 transactions (all, incl. income)
    latest_15 = txs[:15]
    latest_str = "\n".join([
        f"  {t['tx_date']} | {float(t['amount']):>8.2f} ₽ | "
        f"{t['category']!s:<18} | {str(t['description'])[:45]}"
        for t in latest_15
    ])

    # Compose context
    lines = [
        "=== ФИНАНСОВЫЙ КОНТЕКСТ ПОЛЬЗОВАТЕЛЯ (сегодня " + today.isoformat() + ") ===",
        "",
        "БАЛАНС:",
        f"  Основной счёт: {bal:.2f} ₽",
        f"  Резерв: {res:.2f} ₽",
        f"  Копилка: {sav:.2f} ₽",
        f"  Итого: {bal + res + sav:.2f} ₽",
        "",
        f"РАСХОДЫ ЗА ПОСЛЕДНИЕ 7 ДНЕЙ (с {cutoff_7d}):",
        f"  ВСЕГО: {week_total:.0f} ₽",
    ]
    for c, v in sorted(week_by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"    {c}: {v:.0f} ₽")

    lines += [
        "",
        f"РАСХОДЫ ЗА ПОСЛЕДНИЕ 30 ДНЕЙ (с {cutoff_30d}):",
        f"  ВСЕГО: {month_total:.0f} ₽",
    ]
    for c, v in sorted(month_by_cat.items(), key=lambda x: -x[1])[:8]:
        lines.append(f"    {c}: {v:.0f} ₽")

    lines += [
        "",
        "ПОСЛЕДНИЕ 15 ТРАНЗАКЦИЙ (включая доходы):",
        latest_str,
        "",
        "=== КОНЕЦ КОНТЕКСТА ===",
    ]

    return "\n".join(lines)
