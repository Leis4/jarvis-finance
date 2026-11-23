from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.domain import ActionRequest, ActionStatus
from jarvis.finance.reports import render_distribution
from jarvis.finance.rules import DEFAULT_RULES, propose_income_distribution

MONEY_MOVEMENT_ACTIONS = {"income_distribution", "prepared_transfer", "reserve_topup", "savings_topup", "reserve_sweep_to_savings", "goal_contribution_plan"}


def plan_income_distribution(db: FinanceDB, income: Decimal, rules: dict[str, Any]) -> int:
    distribution = propose_income_distribution(income, rules)
    payload = {
        "income": str(income.quantize(Decimal("0.01"))),
        "currency": "RUB",
        "distribution": {key: str(value) for key, value in distribution.items()},
        "human_note": render_distribution(income, rules),
    }
    return db.create_action_request(
        "income_distribution",
        payload,
        reason="Доход распределяется только после ручного подтверждения пользователя.",
        approval_required=True,
    )


def render_action_request(action: ActionRequest) -> str:
    pretty_payload = json.dumps(action.payload, ensure_ascii=False, indent=2)
    return (
        f"Action #{action.id}\n"
        f"type: {action.action_type}\n"
        f"status: {action.status.value}\n"
        f"approval_required: {action.approval_required}\n"
        f"reason: {action.reason}\n"
        f"payload:\n{pretty_payload}"
    )


def approve_action(db: FinanceDB, action_id: int) -> tuple[bool, str]:
    action = db.get_action_request(action_id)
    if action is None:
        return False, f"Action #{action_id} not found."
    if action.status != ActionStatus.PENDING:
        return False, f"Action #{action_id} is not pending; current status: {action.status.value}."
    updated = db.update_action_status(action_id, ActionStatus.APPROVED)
    if not updated:
        return False, f"Action #{action_id} could not be updated."
    if action.action_type in MONEY_MOVEMENT_ACTIONS:
        return True, (
            f"Action #{action_id} approved. В v1.0 это только разрешение плана: "
            "реальный перевод денег не выполняется."
        )
    return True, f"Action #{action_id} approved."


def reject_action(db: FinanceDB, action_id: int) -> tuple[bool, str]:
    action = db.get_action_request(action_id)
    if action is None:
        return False, f"Action #{action_id} not found."
    if action.status != ActionStatus.PENDING:
        return False, f"Action #{action_id} is not pending; current status: {action.status.value}."
    updated = db.update_action_status(action_id, ActionStatus.REJECTED)
    if not updated:
        return False, f"Action #{action_id} could not be updated."
    return True, f"Action #{action_id} rejected."


def execute_local_action(db: FinanceDB, action_id: int) -> tuple[bool, str]:
    """Execute only safe local actions. Never performs real bank transfers."""
    action = db.get_action_request(action_id)
    if action is None:
        return False, f"Action #{action_id} not found."
    if action.status != ActionStatus.APPROVED:
        return False, f"Action #{action_id} must be approved before local execution; current status: {action.status.value}."

    if action.action_type == "goal_contribution_plan":
        from jarvis.finance.goals import apply_goal_progress_action

        count = apply_goal_progress_action(db, action.payload)
        db.update_action_status(action_id, ActionStatus.EXECUTED)
        return True, f"Action #{action_id} executed locally: updated progress for {count} savings goal(s). No bank transfer was made."

    if action.action_type == "budget_limit_update":
        rules = db.get_setting("rules", DEFAULT_RULES)
        category_limits = dict(rules.get("category_limits", DEFAULT_RULES["category_limits"]))
        for category, value in action.payload.get("limits", {}).items():
            category_limits[str(category)] = float(value)
        rules["category_limits"] = category_limits
        db.set_setting("rules", rules)
        db.update_action_status(action_id, ActionStatus.EXECUTED)
        return True, f"Action #{action_id} executed locally: category limits updated in SQLite settings."

    if action.action_type in MONEY_MOVEMENT_ACTIONS:
        return False, (
            f"Action #{action_id} is approved but cannot be executed by v1.0 because it represents money movement. "
            "Use it as a manual checklist only."
        )

    return False, f"Action #{action_id} type '{action.action_type}' has no local executor."
