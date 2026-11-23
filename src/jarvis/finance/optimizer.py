from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.ml_local.spend_predictor import (
    FLEXIBLE_CATEGORIES,
    build_spend_forecast,
)
from jarvis.finance.rules import DEFAULT_RULES, decimal_money


@dataclass(frozen=True)
class OptimizationRow:
    category: str
    current_limit: Decimal
    predicted_spend: Decimal
    recommended_limit: Decimal
    protected_minimum: Decimal
    monthly_savings: Decimal
    priority: str
    reason: str


@dataclass(frozen=True)
class OptimizationPlan:
    engine: str
    forecast_month: str
    reserve_minimum: Decimal
    reserve_balance: Decimal
    reserve_gap: Decimal
    savings_balance: Decimal
    projected_monthly_savings: Decimal
    rows: list[OptimizationRow]
    warnings: list[str]


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def round_to_50(value: Decimal) -> Decimal:
    return ((value / Decimal(50)).quantize(Decimal(1), rounding=ROUND_HALF_UP) * Decimal(50)).quantize(Decimal("0.01"))


def _minimum_for_category(category: str, rules: dict[str, Any], ration_monthly_cost: Decimal) -> Decimal:
    mins = rules.get("category_minimums", DEFAULT_RULES["category_minimums"])
    configured = decimal_money(mins.get(category, 0))
    if category == "food" and ration_monthly_cost > 0:
        # Food is not optimized below the athlete ration baseline plus a small buffer.
        return max(configured, money(ration_monthly_cost * Decimal("1.10")))
    return configured


def build_optimization_plan(db: FinanceDB, rules: dict[str, Any], model_dir: str | None = None) -> OptimizationPlan:
    forecast = build_spend_forecast(db, rules, model_dir=model_dir)
    latest = db.latest_budget_snapshot() or {}
    reserve_minimum = decimal_money(rules.get("reserve_minimum", DEFAULT_RULES["reserve_minimum"]))
    reserve_balance = money(latest.get("reserve_balance", "0"))
    savings_balance = money(latest.get("savings_balance", "0"))
    reserve_gap = max(Decimal("0.00"), reserve_minimum - reserve_balance).quantize(Decimal("0.01"))
    ration_monthly_cost = money(forecast.get("ration_monthly_cost", "0"))

    rows: list[OptimizationRow] = []
    warnings: list[str] = []
    projected_monthly_savings = Decimal("0.00")

    for raw in forecast["categories"]:
        category = str(raw["category"])
        current_limit = money(raw.get("current_limit", "0"))
        predicted_spend = money(raw.get("predicted_spend", "0"))
        protected_minimum = _minimum_for_category(category, rules, ration_monthly_cost)
        base_recommended = money(raw.get("recommended_limit", current_limit))

        if category in FLEXIBLE_CATEGORIES:
            aggressiveness = Decimal("0.82") if reserve_gap > 0 else Decimal("0.88")
            target = predicted_spend * aggressiveness
            priority = "high" if category in {"delivery", "entertainment", "shopping"} else "medium"
            reason = "Гибкая трата: можно ужать без нарушения базовых обязательных расходов."
        elif category == "food":
            target = max(protected_minimum, min(current_limit, predicted_spend * Decimal("0.97")))
            priority = "protected"
            reason = "Еда защищена расчетом рациона: экономия возможна только выше продуктового минимума."
        else:
            target = max(protected_minimum, predicted_spend * Decimal("0.95"))
            priority = "low"
            reason = "Базовая категория: мягкое снижение, чтобы не сломать повседневные расходы."

        recommended = max(protected_minimum, min(current_limit, base_recommended, round_to_50(target)))
        recommended = recommended.quantize(Decimal("0.01"))
        monthly_savings = max(Decimal("0.00"), current_limit - recommended).quantize(Decimal("0.01"))
        projected_monthly_savings += monthly_savings

        if category == "food" and recommended <= protected_minimum and ration_monthly_cost > 0:
            warnings.append("Лимит food уперт в рацион: для дальнейшей экономии нужно менять продуктовую корзину, а не просто снижать лимит.")

        rows.append(
            OptimizationRow(
                category=category,
                current_limit=current_limit,
                predicted_spend=predicted_spend,
                recommended_limit=recommended,
                protected_minimum=protected_minimum,
                monthly_savings=monthly_savings,
                priority=priority,
                reason=reason,
            )
        )

    if reserve_gap > 0 and projected_monthly_savings < reserve_gap:
        warnings.append(
            f"Даже при новых лимитах резерв закрывается не сразу: дефицит {reserve_gap} RUB, прогноз экономии {projected_monthly_savings} RUB/мес."
        )
    if not rows:
        warnings.append("Нет данных для оптимизации. Добавь расходы, категории и snapshot остатков.")

    return OptimizationPlan(
        engine=str(forecast["engine"]),
        forecast_month=str(forecast["forecast_month"]),
        reserve_minimum=reserve_minimum,
        reserve_balance=reserve_balance,
        reserve_gap=reserve_gap,
        savings_balance=savings_balance,
        projected_monthly_savings=projected_monthly_savings.quantize(Decimal("0.01")),
        rows=rows,
        warnings=warnings,
    )


def plan_limit_update_action(db: FinanceDB, plan: OptimizationPlan) -> int:
    payload = {
        "forecast_month": plan.forecast_month,
        "engine": plan.engine,
        "limits": {row.category: str(row.recommended_limit) for row in plan.rows},
        "projected_monthly_savings": str(plan.projected_monthly_savings),
        "reserve_gap": str(plan.reserve_gap),
    }
    return db.create_action_request(
        "budget_limit_update",
        payload,
        reason="Apply optimized category limits after user review.",
        approval_required=True,
    )


def render_optimization_plan(plan: OptimizationPlan) -> str:
    lines = [
        f"Оптимизация бюджета: {plan.forecast_month} (engine={plan.engine})",
        f"Резерв: {plan.reserve_balance} / минимум {plan.reserve_minimum} RUB",
        f"Дефицит резерва: {plan.reserve_gap} RUB",
        f"Накопления: {plan.savings_balance} RUB",
        f"Потенциальная экономия: {plan.projected_monthly_savings} RUB/мес",
        "",
        "Рекомендованные лимиты:",
    ]
    for row in sorted(plan.rows, key=lambda r: (r.priority != "high", r.category)):
        lines.append(
            f"- {row.category}: {row.current_limit} -> {row.recommended_limit} RUB "
            f"(прогноз {row.predicted_spend}, минимум {row.protected_minimum}, экономия {row.monthly_savings}, priority={row.priority})"
        )
        lines.append(f"  {row.reason}")
    if plan.warnings:
        lines.append("")
        lines.append("Предупреждения:")
        lines.extend(f"- {warning}" for warning in plan.warnings)
    return "\n".join(lines)
