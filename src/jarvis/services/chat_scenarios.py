"""Typed chat inputs for existing private capacity and goal calculators."""
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jarvis.finance.review_store import ReviewStore


class ChatScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["hypothetical_goal", "accepted_capacity", "hypothetical_compound", "hypothetical_loan", "hypothetical_inflation"]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    start: date
    end: date | None = None
    target: str | None = Field(default=None, max_length=50)
    starting: str | None = Field(default=None, max_length=50)
    monthly_contribution: str | None = Field(default=None, max_length=50)
    months: int = Field(default=12, ge=1, le=600, strict=True)
    deadline: date | None = None
    contribution_delay_months: int = Field(default=0, ge=0, le=600, strict=True)
    base_spending: str | None = Field(default=None, max_length=50)
    expense_growth_percent: str | None = Field(default=None, max_length=50)
    fact_ids: dict[str, str] = Field(default_factory=dict, max_length=4)
    accepted_source_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    principal: str | None = Field(default=None, max_length=50)
    annual_rate_percent: str | None = Field(default=None, max_length=50)


def calculate_scenario(store: ReviewStore, scenario: ChatScenario, *, now: datetime) -> dict[str, Any]:
    """Calculate locally, revalidating accepted inputs after the capacity computation."""
    from jarvis.bridge.free_cash import capacity_preview
    from jarvis.finance.goals import project_savings_goal
    from jarvis.finance.ledger import finite_money
    from jarvis.finance.review_sources import accepted_capacity_inputs
    if now.tzinfo is None:
        raise ValueError("Aware scenario clock required")
    if scenario.mode in {"hypothetical_compound", "hypothetical_loan", "hypothetical_inflation"}:
        if (scenario.fact_ids or scenario.accepted_source_revision or scenario.target is not None or scenario.starting is not None
                or scenario.monthly_contribution is not None or scenario.end is not None or scenario.deadline is not None
                or scenario.base_spending is not None or scenario.expense_growth_percent is not None or scenario.contribution_delay_months):
            raise ValueError("Calculator assumptions cannot mix with goals or accepted sources")
        if scenario.principal is None or scenario.annual_rate_percent is None:
            return {"status": "insufficient_data", "reason": "Explicit principal and annual rate required", "financial_apply_allowed": False}
        principal, rate = finite_money(scenario.principal), finite_money(scenario.annual_rate_percent)
        if (scenario.currency not in {"RUB", "USD", "EUR"} or principal < 0 or principal > Decimal(10**12)
                or principal != principal.quantize(Decimal("0.01")) or not 0 <= rate <= 100
                or rate != rate.quantize(Decimal("0.0001"))):
            raise ValueError("Bounded principal, currency and annual percentage rate required")
        with localcontext() as ctx:
            ctx.prec = 40
            monthly = rate / 1200
            factor = (1 + monthly) ** scenario.months
            if scenario.mode == "hypothetical_compound":
                value = principal * factor
                output = {"future_amount": str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))}
                formula = "P × (1 + annual_percent / 1200)^months"
                explanation = "Месячная капитализация; взносы, налоги, комиссии и условия реального вклада не моделируются."
            elif scenario.mode == "hypothetical_loan":
                payment = principal / scenario.months if rate == 0 else principal * monthly * factor / (factor - 1)
                total = payment * scenario.months
                output = {"monthly_payment": str(payment.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                          "total_payment": str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                          "interest": str((total - principal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))}
                formula = "P × r × (1+r)^n / ((1+r)^n-1), r=annual_percent/1200; rate=0: P/n"
                explanation = "Теоретический аннуитет. Итог по неокруглённому платежу; банковское округление последнего платежа, страховка и комиссии неизвестны."
            else:
                real = principal / ((1 + rate / 100) ** (Decimal(scenario.months) / 12))
                output = {"purchasing_power": str(real.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))}
                formula = "P / (1 + annual_inflation_percent/100)^(months/12)"
                explanation = "Покупательная способность в ценах начала периода; заданная инфляция — гипотеза, не прогноз."
        return {"status": "hypothetical", "scope": "HYPOTHETICAL", "tool": scenario.mode,
                "currency": scenario.currency, "principal": str(principal), "annual_rate_percent": str(rate),
                "months": scenario.months, "start": scenario.start.isoformat(), "output": output,
                "formula": formula, "explanation": explanation, "fees": None,
                "parameter_source": "explicit hypothetical user inputs", "database_modified": False, "financial_apply_allowed": False}
    if scenario.principal is not None or scenario.annual_rate_percent is not None:
        raise ValueError("Calculator parameters cannot enter goals or accepted capacity")
    if scenario.mode == "hypothetical_goal":
        if scenario.target is None or scenario.starting is None or (scenario.monthly_contribution is None and scenario.deadline is None):
            return {"status": "insufficient_data", "reason": "Explicit target, starting amount and monthly contribution required", "financial_apply_allowed": False}
        if scenario.fact_ids or scenario.accepted_source_revision or scenario.base_spending is not None or scenario.expense_growth_percent is not None or scenario.end is not None:
            raise ValueError("Goal assumptions cannot be mixed with accepted capacity inputs")
        return project_savings_goal(scenario.target, scenario.starting, scenario.monthly_contribution,
            start=scenario.start, months=scenario.months, currency=scenario.currency,
            deadline=scenario.deadline, contribution_delay=scenario.contribution_delay_months)
    if any(v is not None for v in (scenario.target, scenario.starting, scenario.monthly_contribution, scenario.deadline)) or scenario.contribution_delay_months:
        raise ValueError("Capacity cannot invent future income or goal assumptions")
    if scenario.end is None or scenario.accepted_source_revision is None:
        return {"status": "insufficient_data", "reason": "Planning end and accepted source revision required", "financial_apply_allowed": False}
    if (scenario.end - scenario.start).days > 366:
        raise ValueError("Capacity horizon limited to 366 days")
    def current_evidence() -> dict[str, Any]:
        assert scenario.end is not None
        return accepted_capacity_inputs(store, scenario.fact_ids, start=scenario.start, end=scenario.end, currency=scenario.currency, now=now)
    evidence = current_evidence()
    if evidence["status"] != "accepted_evidence":
        return evidence
    if evidence["inputs"]["planning_source_sha256"] != scenario.accepted_source_revision:
        return {"status": "unavailable", "reason": "Accepted source revision changed", "financial_apply_allowed": False}
    base = finite_money(scenario.base_spending) if scenario.base_spending is not None else Decimal(0)
    growth = finite_money(scenario.expense_growth_percent) if scenario.expense_growth_percent is not None else Decimal(0)
    if not 0 <= base <= Decimal(1000000000000000) or not 0 <= growth <= 1000 or (scenario.expense_growth_percent is not None and scenario.base_spending is None):
        raise ValueError("Explicit nonnegative base spending and bounded hypothetical growth required")
    spending = {"before_scenario_spending": Decimal(0)}
    if scenario.base_spending is not None:
        spending.update(base=base, growth=(base * (1 + growth / 100)).quantize(Decimal("0.01")))
    result = capacity_preview(store, scenario.fact_ids, start=scenario.start, end=scenario.end, currency=scenario.currency, now=now,
        scenarios=spending)
    fresh = current_evidence()
    if fresh["status"] != "accepted_evidence" or fresh["inputs"]["planning_source_sha256"] != scenario.accepted_source_revision:
        return {"status": "unavailable", "reason": "Source changed during scenario", "financial_apply_allowed": False}
    return {**result, "assumptions": {"base_spending": scenario.base_spending, "expense_growth_percent": scenario.expense_growth_percent},
            "monthly_saving_capacity": None, "limitations": "One-period capacity; future income and monthly savings are unknown"}
