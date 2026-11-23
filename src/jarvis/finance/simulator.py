from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.rules import decimal_money


@dataclass(frozen=True)
class ProjectionMonth:
    month_number: int
    income: Decimal
    planned_spend: Decimal
    reserve_balance: Decimal
    savings_balance: Decimal
    free_cash: Decimal
    note: str


@dataclass(frozen=True)
class ScenarioProjection:
    months: list[ProjectionMonth]
    total_income: Decimal
    total_planned_spend: Decimal
    total_savings_added: Decimal
    final_reserve: Decimal
    final_savings: Decimal


def simulate_scenario(
    db: FinanceDB,
    rules: dict[str, Any],
    monthly_income: Decimal,
    months: int = 6,
    optimized: bool = False,
) -> ScenarioProjection:
    """Project reserve and savings using current rules.

    This is a deterministic planning simulator, not an investment model. It uses
    the latest factual snapshot if one exists; otherwise it starts from zero.
    """
    if months <= 0:
        raise ValueError("months must be positive")

    snapshot = db.latest_budget_snapshot() or {}
    reserve = decimal_money(snapshot.get("reserve_balance", "0"))
    savings = decimal_money(snapshot.get("savings_balance", "0"))
    reserve_minimum = decimal_money(rules.get("reserve_minimum", 50000))
    savings_percent = Decimal(str(rules.get("income_savings_percent", 20))) / Decimal(100)
    limits = {key: decimal_money(value) for key, value in rules.get("category_limits", {}).items()}

    if optimized:
        # Conservative what-if: assumes the optimizer can reduce flexible limits
        # by 10% without touching hard minimums. Real limit updates still go
        # through pending actions and explicit approval.
        minimums = {key: decimal_money(value) for key, value in rules.get("category_minimums", {}).items()}
        for category, value in list(limits.items()):
            floor = minimums.get(category, Decimal("0.00"))
            limits[category] = max(floor, (value * Decimal("0.90")).quantize(Decimal("0.01")))

    planned_spend = sum(limits.values(), Decimal("0.00")).quantize(Decimal("0.01"))
    rows: list[ProjectionMonth] = []
    total_income = Decimal("0.00")
    total_spend = Decimal("0.00")
    total_savings_added = Decimal("0.00")

    for month_number in range(1, months + 1):
        income = monthly_income.quantize(Decimal("0.01"))
        base_savings = (income * savings_percent).quantize(Decimal("0.01"))
        free_cash = (income - planned_spend - base_savings).quantize(Decimal("0.01"))
        note = "standard"

        if reserve < reserve_minimum:
            needed = (reserve_minimum - reserve).quantize(Decimal("0.01"))
            top_up = min(max(free_cash, Decimal("0.00")), needed)
            reserve += top_up
            free_cash -= top_up
            note = f"reserve_top_up={top_up}"
        elif free_cash > 0:
            # month-end sweep: surplus goes to savings in the projection.
            savings += free_cash
            note = f"sweep_to_savings={free_cash}"
            free_cash = Decimal("0.00")

        savings += base_savings
        total_income += income
        total_spend += planned_spend
        total_savings_added += base_savings
        rows.append(
            ProjectionMonth(
                month_number=month_number,
                income=income,
                planned_spend=planned_spend,
                reserve_balance=reserve.quantize(Decimal("0.01")),
                savings_balance=savings.quantize(Decimal("0.01")),
                free_cash=free_cash.quantize(Decimal("0.01")),
                note=note,
            )
        )

    return ScenarioProjection(
        months=rows,
        total_income=total_income.quantize(Decimal("0.01")),
        total_planned_spend=total_spend.quantize(Decimal("0.01")),
        total_savings_added=total_savings_added.quantize(Decimal("0.01")),
        final_reserve=reserve.quantize(Decimal("0.01")),
        final_savings=savings.quantize(Decimal("0.01")),
    )


def render_projection(projection: ScenarioProjection) -> str:
    lines = ["Scenario projection:"]
    for row in projection.months:
        lines.append(
            f"M{row.month_number}: income={row.income} spend={row.planned_spend} "
            f"reserve={row.reserve_balance} savings={row.savings_balance} free={row.free_cash} note={row.note}"
        )
    lines.append(f"Total income: {projection.total_income}")
    lines.append(f"Total planned spend: {projection.total_planned_spend}")
    lines.append(f"Final reserve: {projection.final_reserve}")
    lines.append(f"Final savings: {projection.final_savings}")
    return "\n".join(lines)
