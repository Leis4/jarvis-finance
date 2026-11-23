from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from jarvis.finance.domain import PurchaseAdvice
from jarvis.finance.ledger import cashflow

DEFAULT_RULES = {
    "reserve_minimum": 50000,
    "income_savings_percent": 20,
    "category_limits": {
        "food": 12000,
        "entertainment": 6000,
        "subscriptions": 2000,
        "transport": 5000,
        "shopping": 8000,
        "delivery": 4000,
    },
    "category_minimums": {
        "food": 3000,
        "transport": 1500,
        "subscriptions": 0,
        "entertainment": 500,
        "shopping": 500,
        "delivery": 0,
    },
    "auto_actions_enabled": False,
    "require_confirmation_for_money_movement": True,
}


@dataclass(frozen=True)
class CategoryStatus:
    category: str
    spent: Decimal
    limit: Decimal
    remaining: Decimal
    used_percent: Decimal
    status: str


def month_bounds(month: str) -> tuple[date, date]:
    year_s, month_s = month.split("-", 1)
    year = int(year_s)
    month_i = int(month_s)
    last = monthrange(year, month_i)[1]
    return date(year, month_i, 1), date(year, month_i, last)


def decimal_money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def category_statuses(transactions: list[dict[str, Any]], rules: dict[str, Any]) -> list[CategoryStatus]:
    limits = rules.get("category_limits", DEFAULT_RULES["category_limits"])
    spent_by_category: dict[str, Decimal] = cashflow(transactions)["by_category"]

    statuses: list[CategoryStatus] = []
    for category, limit_raw in limits.items():
        limit = decimal_money(limit_raw)
        spent = spent_by_category.get(category, Decimal("0.00")).quantize(Decimal("0.01"))
        remaining = (limit - spent).quantize(Decimal("0.01"))
        used = Decimal("0.00") if limit == 0 else ((spent / limit) * 100).quantize(Decimal("0.01"))
        if used >= Decimal(100):
            status = "over_limit"
        elif used >= Decimal(80):
            status = "warning"
        else:
            status = "ok"
        statuses.append(CategoryStatus(category, spent, limit, remaining, used, status))
    return statuses


def propose_income_distribution(income: Decimal, rules: dict[str, Any]) -> dict[str, Decimal]:
    reserve_minimum = decimal_money(rules.get("reserve_minimum", 50000))
    savings_percent = Decimal(str(rules.get("income_savings_percent", 20)))
    savings = (income * savings_percent / Decimal(100)).quantize(Decimal("0.01"))

    food = min(Decimal("6000.00"), income * Decimal("0.20")).quantize(Decimal("0.01"))
    reserve = min(Decimal("5000.00"), income * Decimal("0.17")).quantize(Decimal("0.01"))
    wants = min(Decimal("3000.00"), income * Decimal("0.10")).quantize(Decimal("0.01"))
    investments = savings
    leave = (income - food - reserve - wants - investments).quantize(Decimal("0.01"))

    if leave < 0:
        leave = Decimal("0.00")

    return {
        "food": food,
        "reserve": reserve,
        "wants": wants,
        "investments": investments,
        "leave_on_main": leave,
        "reserve_minimum_target": reserve_minimum,
    }


def advise_purchase(name: str, amount: Decimal, current_balance: Decimal, rules: dict[str, Any]) -> PurchaseAdvice:
    reserve_min = decimal_money(rules.get("reserve_minimum", 50000))
    remaining = (current_balance - amount).quantize(Decimal("0.01"))

    if remaining < 0:
        return PurchaseAdvice(
            name=name,
            amount=amount,
            can_buy=False,
            risk_level="critical",
            message=f"Покупка '{name}' невозможна: не хватает {abs(remaining)} RUB относительно текущего баланса.",
        )

    if remaining < reserve_min:
        return PurchaseAdvice(
            name=name,
            amount=amount,
            can_buy=False,
            risk_level="high",
            message=(
                f"Покупка '{name}' за {amount} RUB опустит остаток до {remaining} RUB, "
                f"ниже целевого резерва {reserve_min} RUB. Лучше отложить или купить после следующего дохода."
            ),
        )

    if remaining < reserve_min * Decimal("1.2"):
        return PurchaseAdvice(
            name=name,
            amount=amount,
            can_buy=True,
            risk_level="medium",
            message=(
                f"Покупку '{name}' можно сделать, но запас после покупки будет {remaining} RUB — близко к минимальному резерву."
            ),
        )

    return PurchaseAdvice(
        name=name,
        amount=amount,
        can_buy=True,
        risk_level="low",
        message=f"Покупка '{name}' выглядит допустимой: после нее останется {remaining} RUB.",
    )
