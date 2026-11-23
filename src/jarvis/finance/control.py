from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.ledger import finite_money
from jarvis.finance.mathops import build_math_forecast_report, qmoney
from jarvis.finance.rules import DEFAULT_RULES, decimal_money

FLEXIBLE_CATEGORIES = {"delivery", "entertainment", "shopping", "subscriptions", "wants", "restaurants"}
ESSENTIAL_CATEGORIES = {"food", "transport", "housing", "health"}


@dataclass(frozen=True)
class CategoryControlRow:
    category: str
    monthly_spend: Decimal
    current_limit: Decimal
    recommended_limit: Decimal
    savings_potential: Decimal
    waste_score: Decimal
    formula: str


@dataclass(frozen=True)
class StressTestReport:
    days: int
    samples: int
    starting_cash: Decimal
    mean_daily_spend: Decimal
    ruin_probability: Decimal
    var95_min_cash: Decimal
    expected_min_cash: Decimal
    formula: str


@dataclass(frozen=True)
class ControlReport:
    current_balance: Decimal
    days_to_income: int
    estimated_daily_burn: Decimal
    cash_runway_days: Decimal
    safe_daily_spend: Decimal
    reserve_gap: Decimal
    total_savings_potential: Decimal
    category_rows: list[CategoryControlRow]
    stress: StressTestReport
    formulas: list[str]


TRANSFER_CATEGORIES = {
    "internal_transfer",
    "transfer",
    "peer_transfer",
    "savings_transfer",
    "income_transfer",
}
COMPENSATION_KEYWORDS = {"интернет", "koptevonet", "провайдер"}


def capacity_scenarios(
    balances: list[dict[str, Any]], expected_accounts: frozenset[str], currency: str,
    obligations: Decimal | None, reserve: Decimal | None, scenarios: dict[str, Decimal], *,
    obligations_confirmed: bool, reserve_confirmed: bool, planning_source_sha256: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compare explicit spending scenarios without predicting income or approving actions."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Aware scenario clock required")
    result: dict[str, Any] = {"status": "insufficient_data", "scenarios": None, "currency": currency,
        "checked_at": current.isoformat(), "database_modified": False,
        "scope": "caller_attested_scenario", "policy_version": "capacity-v1"}
    if (not expected_accounts or not balances or obligations is None or reserve is None
            or obligations_confirmed is not True or reserve_confirmed is not True):
        result["reason"] = "Complete liquid inventory, obligations and reserve acceptance required"
        return result
    try:
        def source_hash(value: Any) -> bool:
            return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
        if not source_hash(planning_source_sha256):
            raise ValueError("Planning source identity required")
        accounts = [row["account"] for row in balances]
        if len(set(accounts)) != len(accounts) or set(accounts) != expected_accounts:
            result["reason"] = "Missing, duplicate or undeclared accounts"
            return result
        if any(row.get("currency") != currency or row.get("coverage_confirmed") is not True
               or row.get("liquid") is not True or row.get("amount") is None for row in balances):
            result["reason"] = "Unknown balance, currency or liquid-source coverage"
            return result
        total = Decimal(0)
        stamps = []
        hashes = []
        components = []
        for row in balances:
            stamp = datetime.fromisoformat(row["as_of"])
            if stamp.tzinfo is None or not source_hash(row.get("source_sha256")):
                raise ValueError("Aware balance time and source identity required")
            if stamp > current or current - stamp > timedelta(hours=24):
                result.update(status="stale", reason="Balance observation expired or future")
                return result
            value = finite_money(row["amount"])
            total += value
            stamps.append(stamp.astimezone(UTC).isoformat())
            hashes.append(row["source_sha256"])
            components.append({"account_sha256": hashlib.sha256(str(row["account"]).encode()).hexdigest(),
                               "amount": str(value), "source_sha256": row["source_sha256"],
                               "as_of": stamp.astimezone(UTC).isoformat()})
        due, protected = finite_money(obligations), finite_money(reserve)
        spending = {name: finite_money(value) for name, value in scenarios.items()}
        if due < 0 or protected < 0 or not spending or any(not name or value < 0 for name, value in spending.items()):
            raise ValueError("Nonnegative obligations/reserve/spending and named scenarios required")
        material = {"balance": str(total), "currency": currency, "obligations": str(due), "reserve": str(protected),
                    "spending": {k: str(v) for k, v in sorted(spending.items())},
                    "balance_sources": sorted(zip(hashes, stamps, strict=True)),
                    "balance_components": sorted(components, key=lambda row: row["account_sha256"]),
                    "planning_source_sha256": planning_source_sha256, "policy_version": "capacity-v1"}
        outcomes = {}
        for name, value in spending.items():
            headroom = total - due - protected - value
            outcomes[name] = {"spending": str(value), "headroom": str(headroom),
                              "saving_capacity": str(max(Decimal(0), headroom)),
                              "shortfall": str(max(Decimal(0), -headroom))}
        result.update(status="experimental", scenarios=outcomes, evidence=material,
            evidence_sha256=hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest(),
            formula="headroom = liquid balances - accepted obligations - designated reserve - scenario spending",
            limitations="Caller-attested sources; no forecast income, probability or production recommendation")
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        result.update(status="unavailable", reason=type(exc).__name__)
    return result


def _recent_expense_by_day(db: FinanceDB, lookback_days: int = 60) -> list[Decimal]:
    rows = db.all_transactions()
    if not rows:
        return []
    latest = max(date.fromisoformat(str(row["tx_date"])[:10]) for row in rows)
    start = latest - timedelta(days=max(1, lookback_days) - 1)
    by_day: dict[date, Decimal] = {start + timedelta(days=i): Decimal("0.00") for i in range((latest - start).days + 1)}
    for row in rows:
        tx_date = date.fromisoformat(str(row["tx_date"])[:10])
        if tx_date < start:
            continue
        category = str(row.get("category") or "uncategorized")
        if category in TRANSFER_CATEGORIES:
            continue
        amount = Decimal(str(row["amount"]))
        if amount < 0:
            by_day[tx_date] += abs(amount)
        elif amount > 0 and category == "peer_transfer":
            desc = str(row.get("description") or "").lower()
            if any(kw in desc for kw in COMPENSATION_KEYWORDS):
                by_day[tx_date] = max(Decimal("0.00"), by_day[tx_date] - amount)
    return [qmoney(value) for _, value in sorted(by_day.items())]


def estimated_daily_burn(db: FinanceDB, lookback_days: int = 60) -> Decimal:
    samples = _recent_expense_by_day(db, lookback_days)
    if not samples:
        return Decimal("0.00")
    # Robust estimate: 70% mean + 30% median to dampen one-off spikes.
    mean = sum(samples, start=Decimal("0.00")) / Decimal(len(samples))
    ordered = sorted(samples)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / Decimal(2)
    return qmoney(mean * Decimal("0.70") + median * Decimal("0.30"))


def monte_carlo_cash_stress(db: FinanceDB, days: int, starting_cash: Decimal | None = None, samples: int = 1000, seed: int = 42) -> StressTestReport:
    daily = _recent_expense_by_day(db, 90)
    if not daily:
        daily = [Decimal("0.00")]
    rng = random.Random(seed)
    cash = qmoney(starting_cash if starting_cash is not None else db.balance())
    min_cash_values: list[Decimal] = []
    ruin_count = 0
    for _ in range(max(1, samples)):
        current = cash
        minimum = cash
        for _day in range(max(1, days)):
            spend = rng.choice(daily)
            current -= spend
            minimum = min(minimum, current)
        min_cash_values.append(qmoney(minimum))
        if minimum < 0:
            ruin_count += 1
    sorted_mins = sorted(min_cash_values)
    var_index = max(0, int(len(sorted_mins) * Decimal("0.05")) - 1)
    expected_min = sum(sorted_mins, start=Decimal("0.00")) / Decimal(len(sorted_mins))
    mean_spend = sum(daily, start=Decimal("0.00")) / Decimal(len(daily))
    return StressTestReport(
        days=days,
        samples=samples,
        starting_cash=cash,
        mean_daily_spend=qmoney(mean_spend),
        ruin_probability=qmoney(Decimal(ruin_count) * Decimal(100) / Decimal(max(1, samples))),
        var95_min_cash=qmoney(sorted_mins[var_index]),
        expected_min_cash=qmoney(expected_min),
        formula="P_ruin = count(min_cash_path < 0) / N; VaR95 = 5th percentile of simulated minimum cash",
    )


def category_spend_current_month(db: FinanceDB) -> dict[str, Decimal]:
    rows = db.all_transactions()
    if not rows:
        return {}
    latest_month = max(str(row["tx_date"])[:7] for row in rows)
    totals: dict[str, Decimal] = {}
    
    # Pre-calculate compensations and expenses
    for row in rows:
        if str(row["tx_date"])[:7] != latest_month:
            continue
        amount = Decimal(str(row["amount"]))
        category = str(row.get("category") or "uncategorized")
        
        if amount > 0 and category == "peer_transfer":
            desc = str(row.get("description") or "").lower()
            if any(kw in desc for kw in COMPENSATION_KEYWORDS):
                totals["bills"] = totals.get("bills", Decimal("0.00")) - amount
            continue
            
        if amount >= 0 or category in TRANSFER_CATEGORIES:
            continue
            
        totals[category] = totals.get(category, Decimal("0.00")) + abs(amount)
        
    return {key: qmoney(max(Decimal("0.00"), value)) for key, value in totals.items()}


def build_control_report(db: FinanceDB, rules: dict[str, Any] | None = None, days_to_income: int = 14, stress_samples: int = 1000) -> ControlReport:
    rules = rules or db.get_setting("rules", DEFAULT_RULES)
    latest_snapshot = db.latest_budget_snapshot()
    if latest_snapshot and float(latest_snapshot.get("cash_balance", 0)) > 0:
        balance = qmoney(Decimal(str(latest_snapshot["cash_balance"])))
    else:
        balance = qmoney(db.balance())
        
    math_report = build_math_forecast_report(db, rules)
    reserve_minimum = decimal_money(rules.get("reserve", {}).get("minimum", 50000))
    reserve_balance = Decimal(str(latest_snapshot["reserve_balance"])) if latest_snapshot else Decimal("0.00")
    reserve_gap = qmoney(max(Decimal("0.00"), reserve_minimum - reserve_balance))
    burn = estimated_daily_burn(db)
    safe_daily = qmoney(max(Decimal("0.00"), balance) / Decimal(max(1, days_to_income)))
    runway = qmoney((balance / burn) if burn > 0 else Decimal("999.00"))
    spends = category_spend_current_month(db)
    rows: list[CategoryControlRow] = []
    total_potential = Decimal("0.00")
    by_cat = {row.category: row for row in math_report.categories}
    for category in sorted(set(spends) | set(by_cat)):
        math_row = by_cat.get(category)
        current_limit = math_row.current_limit if math_row else Decimal("0.00")
        recommended = math_row.recommended_limit if math_row else current_limit
        spend = spends.get(category, Decimal("0.00"))
        potential = max(Decimal("0.00"), current_limit - recommended)
        total_potential += potential
        flexibility = Decimal("1.00") if category in FLEXIBLE_CATEGORIES else Decimal("0.35") if category not in ESSENTIAL_CATEGORIES else Decimal("0.10")
        limit_base = max(current_limit, Decimal("1.00"))
        spend_ratio = min(Decimal("3.00"), spend / limit_base)
        risk = (math_row.overspend_risk / Decimal(100)) if math_row else Decimal("0.00")
        waste_score = qmoney(Decimal(100) * flexibility * (Decimal("0.55") * spend_ratio + Decimal("0.45") * risk))
        rows.append(
            CategoryControlRow(
                category=category,
                monthly_spend=spend,
                current_limit=current_limit,
                recommended_limit=recommended,
                savings_potential=qmoney(potential),
                waste_score=waste_score,
                formula="waste_score = 100 * flexibility * (0.55*spend/limit + 0.45*overspend_risk)",
            )
        )
    rows.sort(key=lambda row: (row.waste_score, row.savings_potential), reverse=True)
    stress = monte_carlo_cash_stress(db, days_to_income, starting_cash=balance, samples=stress_samples)
    return ControlReport(
        current_balance=balance,
        days_to_income=days_to_income,
        estimated_daily_burn=burn,
        cash_runway_days=runway,
        safe_daily_spend=safe_daily,
        reserve_gap=reserve_gap,
        total_savings_potential=qmoney(total_potential),
        category_rows=rows,
        stress=stress,
        formulas=[
            "daily_burn = 0.70*mean(last_60_daily_expenses) + 0.30*median(last_60_daily_expenses)",
            "safe_daily_spend = max(0, balance) / days_to_income",
            "cash_runway_days = balance / estimated_daily_burn",
            "forecast = 0.60*EMA + 0.40*WMA(3)",
            "reserve_gap = max(0, reserve_minimum - reserve_balance)",
            "waste_score = 100 * flexibility * (0.55*spend/limit + 0.45*overspend_risk)",
            "P_ruin = count(min_cash_path < 0) / MonteCarlo_samples",
        ],
    )


def render_stress_test(report: StressTestReport) -> str:
    return "\n".join(
        [
            "Monte Carlo cash stress test",
            f"days: {report.days}",
            f"samples: {report.samples}",
            f"starting_cash: {report.starting_cash} RUB",
            f"mean_daily_spend: {report.mean_daily_spend} RUB",
            f"ruin_probability: {report.ruin_probability}%",
            f"VaR95 minimum cash: {report.var95_min_cash} RUB",
            f"expected minimum cash: {report.expected_min_cash} RUB",
            f"formula: {report.formula}",
        ]
    )


def render_control_report(report: ControlReport) -> str:
    lines = [
        "AI Finance Control Report",
        f"balance: {report.current_balance} RUB",
        f"days_to_income: {report.days_to_income}",
        f"estimated_daily_burn: {report.estimated_daily_burn} RUB",
        f"cash_runway_days: {report.cash_runway_days}",
        f"safe_daily_spend: {report.safe_daily_spend} RUB",
        f"reserve_gap: {report.reserve_gap} RUB",
        f"total_savings_potential: {report.total_savings_potential} RUB",
        f"stress_ruin_probability: {report.stress.ruin_probability}%",
        "",
        "Formulas:",
    ]
    lines.extend(f"- {formula}" for formula in report.formulas)
    lines.append("")
    lines.append("Category control:")
    for row in report.category_rows[:20]:
        lines.append(
            f"- {row.category}: spent={row.monthly_spend}, limit={row.current_limit}->{row.recommended_limit}, "
            f"save={row.savings_potential}, waste_score={row.waste_score}"
        )
    return "\n".join(lines)
