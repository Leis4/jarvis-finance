from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.ledger import finite_money

CENT = Decimal("0.01")


def project_savings_goal(target: str, starting: str, contribution: str | None, *, start: date,
                         months: int, currency: str, deadline: date | None = None,
                         contribution_delay: int = 0) -> dict[str, Any]:
    """Project explicit hypothetical savings, without opening or modifying a ledger."""
    amounts = [finite_money(v) for v in (target, starting) + ((contribution,) if contribution is not None else ())]
    if any(v < 0 or v > Decimal(1000000000000000) or v != v.quantize(CENT) for v in amounts):
        raise ValueError("Nonnegative cent-precision scenario amounts required")
    if currency not in {"RUB", "USD", "EUR"}:
        raise ValueError("Unsupported scenario currency; no FX")
    if type(months) is not int or not 1 <= months <= 600 or type(contribution_delay) is not int or not 0 <= contribution_delay <= months:
        raise ValueError("Bounded monthly planning horizon required")
    if deadline is not None and deadline < start:
        raise ValueError("Deadline precedes planning start")
    goal, initial = amounts[:2]
    monthly = amounts[2] if contribution is not None else None
    def anniversary(offset: int) -> date:
        index = start.year * 12 + start.month - 1 + offset
        year, month = divmod(index, 12)
        return date(year, month + 1, min(start.day, monthrange(year, month + 1)[1]))
    dates = [anniversary(n) for n in range(1, months + 1)]
    if deadline is not None and deadline > dates[-1]:
        raise ValueError("Deadline exceeds the explicit planning horizon")
    slots = sum(d <= deadline for d in dates[contribution_delay:]) if deadline is not None else None
    gap = max(Decimal(0), goal - initial)
    payments = int((gap / monthly).to_integral_value(rounding=ROUND_CEILING)) if monthly is not None and monthly > 0 else None
    required = 0 if gap == 0 else payments + contribution_delay if payments is not None else None
    achieved = required is not None and required <= months
    at_deadline = initial + monthly * slots if slots is not None and monthly is not None else None
    return {"status": "hypothetical", "scope": "HYPOTHETICAL", "currency": currency,
            "target": str(goal), "starting": str(initial), "monthly_contribution": str(monthly) if monthly is not None else None,
            "period": [start.isoformat(), dates[-1].isoformat()], "deadline": deadline.isoformat() if deadline else None,
            "contribution_delay_months": contribution_delay,
            "months_to_goal": required if achieved else None,
            "goal_date": (start if required == 0 else dates[required - 1]).isoformat() if achieved and required is not None else None,
            "reachable_in_horizon": achieved if monthly is not None or gap == 0 else None, "projected_amount": str(initial + monthly * (months - contribution_delay)) if monthly is not None else None,
            "deadline_amount": str(at_deadline) if at_deadline is not None else None,
            "deadline_met": at_deadline >= goal if at_deadline is not None else None,
            "required_monthly": str((gap / slots).quantize(CENT, rounding=ROUND_CEILING)) if slots else "0.00" if gap == 0 else None,
            "formula": "required_monthly = max(0, target - starting) / remaining monthly slots, rounded up to cents" if monthly is None else "starting + contribution * completed contribution months; no interest, FX or income forecast",
            "parameter_source": "explicit hypothetical user inputs; not accepted balances or a saved goal",
            "database_modified": False, "financial_apply_allowed": False}


@dataclass(frozen=True)
class GoalAllocation:
    goal_id: int
    name: str
    amount: Decimal
    remaining_before: Decimal
    remaining_after: Decimal
    priority: int


@dataclass(frozen=True)
class GoalPlan:
    available_amount: Decimal
    allocated_amount: Decimal
    unallocated_amount: Decimal
    allocations: list[GoalAllocation]
    note: str


def _d(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


def goal_progress(goal: dict[str, Any]) -> Decimal:
    target = _d(goal["target_amount"])
    if target <= 0:
        return Decimal("0.00")
    return min(Decimal("100.00"), (_d(goal["current_amount"]) / target * Decimal(100)).quantize(CENT))


def build_goal_contribution_plan(db: FinanceDB, available_amount: Decimal) -> GoalPlan:
    """Allocate savings surplus to active goals by priority, deadline, and remaining amount.

    This is deterministic and stdlib-only. An optional OR-Tools solver can be
    added later for richer constraints, but the current version intentionally
    remains auditable: the user can see exactly why each goal receives money.
    """
    available = max(Decimal("0.00"), available_amount.quantize(CENT))
    remaining_cash = available
    allocations: list[GoalAllocation] = []
    goals = db.list_savings_goals(status="active")
    for goal in goals:
        if remaining_cash <= 0:
            break
        target = _d(goal["target_amount"])
        current = _d(goal["current_amount"])
        remaining_goal = max(Decimal("0.00"), target - current)
        if remaining_goal <= 0:
            continue
        allocation = min(remaining_cash, remaining_goal).quantize(CENT, rounding=ROUND_DOWN)
        if allocation <= 0:
            continue
        allocations.append(
            GoalAllocation(
                goal_id=int(goal["id"]),
                name=str(goal["name"]),
                amount=allocation,
                remaining_before=remaining_goal,
                remaining_after=(remaining_goal - allocation).quantize(CENT),
                priority=int(goal["priority"]),
            )
        )
        remaining_cash = (remaining_cash - allocation).quantize(CENT)
    allocated = sum((a.amount for a in allocations), Decimal("0.00")).quantize(CENT)
    note = "No active savings goals found." if not goals else "Goal allocation plan built. Review before moving real money."
    return GoalPlan(available, allocated, (available - allocated).quantize(CENT), allocations, note)


def plan_goal_contributions(db: FinanceDB, available_amount: Decimal) -> int:
    plan = build_goal_contribution_plan(db, available_amount)
    payload = {
        "available_amount": str(plan.available_amount),
        "allocated_amount": str(plan.allocated_amount),
        "unallocated_amount": str(plan.unallocated_amount),
        "allocations": [
            {
                "goal_id": allocation.goal_id,
                "name": allocation.name,
                "amount": str(allocation.amount),
                "remaining_before": str(allocation.remaining_before),
                "remaining_after": str(allocation.remaining_after),
                "priority": allocation.priority,
            }
            for allocation in plan.allocations
        ],
        "human_note": render_goal_plan(plan),
    }
    return db.create_action_request(
        "goal_contribution_plan",
        payload,
        reason="Это только план распределения свободных денег по целям; реальные переводы выполняются вручную.",
        approval_required=True,
    )


def apply_goal_progress_action(db: FinanceDB, action_payload: dict[str, Any]) -> int:
    """Apply a goal contribution plan to local goal progress after manual confirmation.

    This updates only local SQLite goal progress. It does not access banks.
    """
    count = 0
    for item in action_payload.get("allocations", []):
        amount = _d(item.get("amount", "0"))
        if amount <= 0:
            continue
        db.add_goal_contribution(
            goal_id=int(item["goal_id"]),
            amount=amount,
            contribution_date=date.today(),  # noqa: DTZ011
            source="approved_plan",
            notes="Applied from approved local goal_contribution_plan action.",
        )
        count += 1
    return count


def render_goals(goals: list[dict[str, Any]]) -> str:
    if not goals:
        return "No savings goals yet."
    lines = ["Savings goals:"]
    for goal in goals:
        progress = goal_progress(goal)
        deadline = goal.get("deadline") or "no deadline"
        lines.append(
            f"#{goal['id']} {goal['name']} | {goal['current_amount']}/{goal['target_amount']} RUB "
            f"({progress}%) | priority={goal['priority']} | deadline={deadline} | status={goal['status']}"
        )
    return "\n".join(lines)


def render_goal_plan(plan: GoalPlan) -> str:
    lines = [
        "Goal contribution plan:",
        f"available: {plan.available_amount} RUB",
        f"allocated: {plan.allocated_amount} RUB",
        f"unallocated: {plan.unallocated_amount} RUB",
    ]
    if not plan.allocations:
        lines.append(plan.note)
        return "\n".join(lines)
    for allocation in plan.allocations:
        lines.append(
            f"- #{allocation.goal_id} {allocation.name}: +{allocation.amount} RUB "
            f"remaining {allocation.remaining_before} -> {allocation.remaining_after}"
        )
    lines.append("No real bank transfer is performed by this plan.")
    return "\n".join(lines)
