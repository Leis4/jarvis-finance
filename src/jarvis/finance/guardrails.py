from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from jarvis.finance.db import FinanceDB

CENT = Decimal("0.01")
FLEXIBLE_CATEGORIES = {"entertainment", "delivery", "coffee", "taxi", "marketplace", "shopping", "games", "subscriptions"}


@dataclass(frozen=True)
class DailyGuardrail:
    today: date
    days_to_income: int
    cash_balance: Decimal
    protected_reserve_gap: Decimal
    essential_buffer: Decimal
    free_cash: Decimal
    daily_safe_spend: Decimal
    freeze_categories: list[str]
    message: str


def _d(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


def build_daily_guardrail(db: FinanceDB, rules: dict[str, Any], days_to_income: int, today: date | None = None) -> DailyGuardrail:
    today = today or date.today()  # noqa: DTZ011
    snapshot = db.latest_budget_snapshot()
    if snapshot is None:
        cash_balance = db.balance()
        reserve_balance = Decimal("0.00")
    else:
        cash_balance = _d(snapshot["cash_balance"])
        reserve_balance = _d(snapshot["reserve_balance"])

    reserve_minimum = _d(rules.get("reserve", {}).get("minimum", "50000"))
    protected_reserve_gap = max(Decimal("0.00"), reserve_minimum - reserve_balance).quantize(CENT)

    category_limits = rules.get("category_limits", {})
    essential_limit = sum(_d(category_limits.get(key, 0)) for key in ("food", "transport", "rent", "utilities"))
    # Use a conservative partial essential buffer until next income.
    month_days = Decimal(30)
    essential_buffer = (essential_limit * Decimal(max(1, days_to_income)) / month_days).quantize(CENT)

    free_cash = max(Decimal("0.00"), cash_balance - protected_reserve_gap - essential_buffer).quantize(CENT)
    daily_safe_spend = (free_cash / Decimal(max(1, days_to_income))).quantize(CENT)

    month_start = today.replace(day=1)
    transactions = db.transactions_between(month_start, today)
    spent_by_category: dict[str, Decimal] = {}
    for tx in transactions:
        amount = _d(tx["amount"])
        if amount >= 0:
            continue
        cat = str(tx["category"])
        spent_by_category[cat] = spent_by_category.get(cat, Decimal("0.00")) + abs(amount)
    freeze_categories: list[str] = []
    for category, spent in spent_by_category.items():
        limit = _d(category_limits.get(category, 0))
        if category in FLEXIBLE_CATEGORIES and limit > 0 and spent >= limit * Decimal("0.80"):
            freeze_categories.append(category)
    freeze_categories = sorted(set(freeze_categories))

    if free_cash <= 0:
        message = "Жесткий режим: свободных денег сверх резерва и базовых расходов почти нет."
    elif daily_safe_spend < Decimal(300):
        message = "Осторожный режим: дневной лимит низкий, лучше заморозить необязательные покупки."
    else:
        message = "Нормальный режим: держись дневного лимита и не трогай резерв."

    return DailyGuardrail(
        today=today,
        days_to_income=max(1, days_to_income),
        cash_balance=cash_balance,
        protected_reserve_gap=protected_reserve_gap,
        essential_buffer=essential_buffer,
        free_cash=free_cash,
        daily_safe_spend=daily_safe_spend,
        freeze_categories=freeze_categories,
        message=message,
    )


def save_daily_guardrail(db: FinanceDB, guardrail: DailyGuardrail) -> int:
    return db.add_budget_guardrail(
        guardrail_date=guardrail.today,
        days_to_income=guardrail.days_to_income,
        free_cash=guardrail.free_cash,
        daily_safe_spend=guardrail.daily_safe_spend,
        recommended_freeze_categories=guardrail.freeze_categories,
    )


def render_daily_guardrail(guardrail: DailyGuardrail) -> str:
    lines = [
        "Daily spending guardrail:",
        f"date: {guardrail.today.isoformat()}",
        f"days_to_income: {guardrail.days_to_income}",
        f"cash_balance: {guardrail.cash_balance} RUB",
        f"protected_reserve_gap: {guardrail.protected_reserve_gap} RUB",
        f"essential_buffer: {guardrail.essential_buffer} RUB",
        f"free_cash: {guardrail.free_cash} RUB",
        f"daily_safe_spend: {guardrail.daily_safe_spend} RUB",
        f"message: {guardrail.message}",
    ]
    if guardrail.freeze_categories:
        lines.append("freeze_categories: " + ", ".join(guardrail.freeze_categories))
    else:
        lines.append("freeze_categories: none")
    return "\n".join(lines)
