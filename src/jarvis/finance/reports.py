from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from jarvis.finance.ledger import cashflow
from jarvis.finance.rules import category_statuses, propose_income_distribution


def monthly_report(transactions: list[dict[str, Any]], month: str, rules: dict[str, Any]) -> dict[str, Any]:
    selected = [tx for tx in transactions if str(tx["tx_date"])[:7] == month]
    flow = cashflow(selected)
    income, expenses, by_category = flow["income"], flow["expenses"], flow["by_category"]

    net = (income - expenses).quantize(Decimal("0.01"))
    statuses = category_statuses(selected, rules)
    warnings = [s for s in statuses if s.status != "ok"]

    return {
        "month": month,
        "income": income.quantize(Decimal("0.01")),
        "expenses": expenses.quantize(Decimal("0.01")),
        "net": net,
        "by_category": {k: v.quantize(Decimal("0.01")) for k, v in sorted(by_category.items())},
        "category_statuses": statuses,
        "warnings": warnings,
    }


def forecast(transactions: list[dict[str, Any]], days: int, expected_income: Decimal | None = None) -> dict[str, Any]:
    if days <= 0:
        raise ValueError("days must be positive")

    if transactions:
        latest = max(date.fromisoformat(tx["tx_date"]) for tx in transactions)
    else:
        latest = date.today()  # noqa: DTZ011
    start = latest - timedelta(days=days - 1)

    recent_expense = Decimal("0.00")
    for tx in transactions:
        tx_date = date.fromisoformat(tx["tx_date"])
        amount = Decimal(str(tx["amount"]))
        if start <= tx_date <= latest and amount < 0:
            recent_expense += abs(amount)

    avg_daily_expense = (recent_expense / Decimal(days)).quantize(Decimal("0.01"))
    projected_expense = (avg_daily_expense * Decimal(days)).quantize(Decimal("0.01"))

    total_balance = sum((Decimal(str(tx["amount"])) for tx in transactions), start=Decimal("0.00")).quantize(Decimal("0.01"))
    if expected_income:
        total_balance += expected_income
    projected_balance = (total_balance - projected_expense).quantize(Decimal("0.01"))

    return {
        "window_days": days,
        "recent_expense": recent_expense.quantize(Decimal("0.01")),
        "avg_daily_expense": avg_daily_expense,
        "projected_expense_next_window": projected_expense,
        "balance_now_with_expected_income": total_balance.quantize(Decimal("0.01")),
        "projected_balance_after_window": projected_balance,
    }


def render_money(value: Decimal | str | float) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01'))} RUB"


def render_monthly_report(report: dict[str, Any]) -> str:
    lines = [
        f"Отчет за {report['month']}",
        f"Доходы: {render_money(report['income'])}",
        f"Расходы: {render_money(report['expenses'])}",
        f"Итог: {render_money(report['net'])}",
        "",
        "Расходы по категориям:",
    ]
    for category, amount in report["by_category"].items():
        lines.append(f"- {category}: {render_money(amount)}")

    lines.append("")
    lines.append("Лимиты:")
    for status in report["category_statuses"]:
        lines.append(
            f"- {status.category}: {render_money(status.spent)} / {render_money(status.limit)} "
            f"({status.used_percent}%, {status.status})"
        )

    if report["warnings"]:
        lines.append("")
        lines.append("Внимание:")
        for warning in report["warnings"]:
            lines.append(f"- {warning.category}: статус {warning.status}")
    return "\n".join(lines)


def render_distribution(income: Decimal, rules: dict[str, Any]) -> str:
    distribution = propose_income_distribution(income, rules)
    lines = [f"План распределения дохода {render_money(income)}:"]
    for key, value in distribution.items():
        if key == "reserve_minimum_target":
            continue
        lines.append(f"- {key}: {render_money(value)}")
    lines.append("Действия не выполняются автоматически. Требуется подтверждение пользователя.")
    return "\n".join(lines)
