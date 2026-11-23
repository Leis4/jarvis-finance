from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.rules import DEFAULT_RULES, decimal_money


@dataclass(frozen=True)
class CategoryMathRow:
    category: str
    samples: list[Decimal]
    latest: Decimal
    mean: Decimal
    stddev: Decimal
    ema_forecast: Decimal
    weighted_forecast: Decimal
    combined_forecast: Decimal
    current_limit: Decimal
    recommended_limit: Decimal
    overspend_risk: Decimal
    reserve_transfer_potential: Decimal
    formula: str
    reason: str


@dataclass(frozen=True)
class MathForecastReport:
    alpha: Decimal
    tighten_factor: Decimal
    categories: list[CategoryMathRow]
    total_current_limits: Decimal
    total_recommended_limits: Decimal
    total_potential_savings: Decimal
    reserve_required: Decimal
    reserve_formula: str


def qmoney(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def round_to_step(value: Decimal, step: Decimal = Decimal(100)) -> Decimal:
    if step <= 0:
        return qmoney(value)
    return ((value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step).quantize(Decimal("0.01"))


def sigmoid(x: float) -> float:
    # Guard against overflow for tiny local Python installs.
    if x >= 60:
        return 1.0
    if x <= -60:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def month_category_series(db: FinanceDB) -> dict[str, dict[str, Decimal]]:
    months: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0.00")))
    for tx in db.all_transactions():
        amount = Decimal(str(tx["amount"]))
        if amount >= 0:
            continue
        month = str(tx["tx_date"])[:7]
        category = tx.get("category") or "uncategorized"
        months[month][category] += abs(amount)
    for entry in db.list_category_spending_entries(limit=10000):
        month = str(entry["entry_date"])[:7]
        category = entry.get("category") or "uncategorized"
        months[month][category] += Decimal(str(entry["amount_spent"]))
    return {month: {cat: qmoney(value) for cat, value in cats.items()} for month, cats in months.items()}


def category_series(db: FinanceDB, category: str) -> list[Decimal]:
    monthly = month_category_series(db)
    if not monthly:
        return []
    months = sorted(monthly)
    return [qmoney(monthly[month].get(category, Decimal("0.00"))) for month in months]


def ema(values: list[Decimal], alpha: Decimal) -> Decimal:
    if not values:
        return Decimal("0.00")
    alpha = max(Decimal("0.01"), min(Decimal("0.99"), alpha))
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (Decimal("1.00") - alpha) * result
    return qmoney(result)


def weighted_moving_average(values: list[Decimal], window: int = 3) -> Decimal:
    if not values:
        return Decimal("0.00")
    selected = values[-max(1, window):]
    weights = list(range(1, len(selected) + 1))
    numerator = sum((value * Decimal(weight) for value, weight in zip(selected, weights)), start=Decimal("0.00"))
    denominator = Decimal(sum(weights))
    return qmoney(numerator / denominator)


def stddev(values: list[Decimal]) -> Decimal:
    if len(values) <= 1:
        return Decimal("0.00")
    mean = sum(values, start=Decimal("0.00")) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), start=Decimal("0.00")) / Decimal(len(values) - 1)
    return qmoney(Decimal(str(math.sqrt(float(variance)))))


def minimum_limit(category: str, rules: dict[str, Any], db: FinanceDB) -> Decimal:
    minimums = rules.get("category_minimums", DEFAULT_RULES["category_minimums"])
    floor = decimal_money(minimums.get(category, 0))
    if category == "food":
        ration_floor = qmoney(db.ration_daily_cost() * Decimal(30) * Decimal("1.10"))
        floor = max(floor, ration_floor)
    return floor


def category_tighten_factor(category: str, base: Decimal) -> Decimal:
    if category in {"delivery", "entertainment", "shopping", "subscriptions", "wants"}:
        return min(Decimal("0.35"), base + Decimal("0.10"))
    if category in {"food", "transport", "health"}:
        return max(Decimal("0.03"), base - Decimal("0.07"))
    return base


def recommend_limit_from_forecast(category: str, forecast: Decimal, current_limit: Decimal, rules: dict[str, Any], db: FinanceDB, tighten_factor: Decimal) -> Decimal:
    floor = minimum_limit(category, rules, db)
    cat_tighten = category_tighten_factor(category, tighten_factor)
    raw = forecast * (Decimal("1.00") - cat_tighten)
    if current_limit > 0:
        raw = min(raw, current_limit)
    return round_to_step(max(floor, raw))


def overspend_probability(forecast: Decimal, limit: Decimal, sigma: Decimal) -> Decimal:
    if limit <= 0:
        return Decimal("0.00")
    denom = max(float(sigma), float(limit) * 0.10, 1.0)
    score = sigmoid((float(forecast) - float(limit)) / denom)
    return Decimal(str(score * 100)).quantize(Decimal("0.01"))


def reserve_requirement(db: FinanceDB, rules: dict[str, Any], days: int = 30, z_score: Decimal = Decimal("1.28")) -> Decimal:
    # Formula: R = essential_daily * D + z * sigma_daily * sqrt(D)
    limits = rules.get("category_limits", DEFAULT_RULES["category_limits"])
    essential_monthly = Decimal("0.00")
    for category in ("food", "transport", "housing", "health"):
        essential_monthly += decimal_money(limits.get(category, 0))
    essential_daily = essential_monthly / Decimal(30)
    daily_samples: list[Decimal] = []
    by_date: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for tx in db.all_transactions():
        amount = Decimal(str(tx["amount"]))
        if amount < 0 and (tx.get("category") or "") in {"food", "transport", "housing", "health"}:
            by_date[str(tx["tx_date"])] += abs(amount)
    daily_samples = list(by_date.values())
    sigma = stddev(daily_samples) if len(daily_samples) > 1 else essential_daily * Decimal("0.25")
    required = essential_daily * Decimal(days) + z_score * sigma * Decimal(str(math.sqrt(days)))
    return qmoney(required)


def build_math_forecast_report(
    db: FinanceDB,
    rules: dict[str, Any],
    alpha: Decimal = Decimal("0.45"),
    tighten_factor: Decimal = Decimal("0.15"),
) -> MathForecastReport:
    limits = rules.get("category_limits", DEFAULT_RULES["category_limits"])
    monthly = month_category_series(db)
    categories = sorted(set(limits) | {category for cats in monthly.values() for category in cats})
    rows: list[CategoryMathRow] = []
    total_current = Decimal("0.00")
    total_recommended = Decimal("0.00")
    total_savings = Decimal("0.00")
    for category in categories:
        values = category_series(db, category)
        current_limit = decimal_money(limits.get(category, 0))
        mean = qmoney(sum(values, start=Decimal("0.00")) / Decimal(len(values))) if values else Decimal("0.00")
        sigma = stddev(values)
        e = ema(values, alpha)
        w = weighted_moving_average(values)
        combined = qmoney(e * Decimal("0.60") + w * Decimal("0.40"))
        if not values and current_limit > 0:
            combined = qmoney(current_limit * Decimal("0.50"))
        recommended = recommend_limit_from_forecast(category, combined, current_limit, rules, db, tighten_factor)
        risk = overspend_probability(combined, current_limit, sigma)
        potential = max(Decimal("0.00"), current_limit - recommended).quantize(Decimal("0.01"))
        total_current += current_limit
        total_recommended += recommended
        total_savings += potential
        reason = ""
        if category in {"delivery", "entertainment", "shopping", "subscriptions", "wants"}:
            reason = "Гибкая категория: ужимаем сильнее, чтобы высвободить деньги под резерв и цели."
        elif category == "food":
            reason = "Еда ограничена нижним порогом: учитывается расчетная стоимость рациона."
        else:
            reason = "Консервативная рекомендация по фактической динамике расходов."
        rows.append(
            CategoryMathRow(
                category=category,
                samples=values,
                latest=values[-1] if values else Decimal("0.00"),
                mean=mean,
                stddev=sigma,
                ema_forecast=e,
                weighted_forecast=w,
                combined_forecast=combined,
                current_limit=current_limit,
                recommended_limit=recommended,
                overspend_risk=risk,
                reserve_transfer_potential=potential,
                formula="forecast = 0.60*EMA(alpha) + 0.40*WMA(3); risk = sigmoid((forecast-limit)/sigma)",
                reason=reason,
            )
        )
    reserve = reserve_requirement(db, rules)
    return MathForecastReport(
        alpha=alpha,
        tighten_factor=tighten_factor,
        categories=rows,
        total_current_limits=qmoney(total_current),
        total_recommended_limits=qmoney(total_recommended),
        total_potential_savings=qmoney(total_savings),
        reserve_required=reserve,
        reserve_formula="R = essential_daily*days + z*sigma_daily*sqrt(days), z=1.28",
    )


def render_math_forecast_report(report: MathForecastReport) -> str:
    lines = [
        "Mathematical budget forecast",
        f"alpha={report.alpha}, tighten_factor={report.tighten_factor}",
        "Formula: forecast = 0.60*EMA + 0.40*WMA(3)",
        "Formula: overspend_risk = sigmoid((forecast - limit) / max(stddev, 10% limit))",
        "Formula: recommended_limit = max(floor, min(current_limit, forecast*(1-tighten)))",
        f"Reserve formula: {report.reserve_formula}",
        f"Required reserve estimate: {report.reserve_required} RUB",
        f"Current limits total: {report.total_current_limits} RUB",
        f"Recommended limits total: {report.total_recommended_limits} RUB",
        f"Potential monthly transfer to reserve/savings: {report.total_potential_savings} RUB",
        "",
        "Categories:",
    ]
    for row in report.categories:
        lines.append(
            f"- {row.category}: latest={row.latest}, mean={row.mean}, std={row.stddev}, "
            f"ema={row.ema_forecast}, wma={row.weighted_forecast}, forecast={row.combined_forecast}, "
            f"limit={row.current_limit}->{row.recommended_limit}, risk={row.overspend_risk}%, free={row.reserve_transfer_potential}"
        )
        lines.append(f"  {row.reason}")
    return "\n".join(lines)
