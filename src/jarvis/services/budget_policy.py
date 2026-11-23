"""Nested role/job budgets over the existing BudgetStore; no second manager."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from jarvis.core.llm.budget_store import BudgetReservationDenied, BudgetStore
from jarvis.services.roles import REGISTRY

# Role allowances are nested caps inside the parent purpose quota, never extra money.
ROLE_ALLOWANCE_RUB: dict[str, str] = {
    'F01': '180', 'F03': '60', 'F06': '10', 'F08': '15',
    'P01': '80', 'P02': '30', 'P03': '15', 'P05': '10', 'P06': '5',
    'M01': '20', 'M02': '15', 'M04': '15',
}


@dataclass(frozen=True)
class JobBudgetContract:
    job_id: str
    role_id: str
    purpose: str
    model: str
    ceiling_rub: str
    max_turns: int
    idempotency_key: str
    input_revision: str
    expires_at: str
    consent_scope: str


def role_budget(role_id: str, store: BudgetStore, *, now: datetime | None = None) -> dict[str, Any]:
    role = REGISTRY[role_id]
    report = store.global_report(now=now or datetime.now(UTC))
    if not report.get('purpose_quotas'):
        store.enable_purpose_limits()
        report = store.global_report(now=now or datetime.now(UTC))
    parent = report['purpose_quotas'][role.purpose]
    allowance = Decimal(ROLE_ALLOWANCE_RUB[role_id])
    remaining = Decimal(parent['remaining_rub'])
    effective = min(allowance, remaining, Decimal(role.max_cost_rub))
    return {'role_id': role_id, 'purpose': role.purpose, 'model': role.model,
            'allowance_rub': str(allowance), 'parent_remaining_rub': str(remaining),
            'effective_ceiling_rub': str(effective), 'max_turns': role.max_turns}


def effective_job_ceiling(role_id: str, store: BudgetStore, *, now: datetime | None = None) -> Decimal:
    return Decimal(role_budget(role_id, store, now=now)['effective_ceiling_rub'])


def job_contract(role_id: str, *, job_id: str, idempotency_key: str, input_revision: str,
                 expires_at: str, consent_scope: str, now: datetime | None = None,
                 store: BudgetStore | None = None) -> JobBudgetContract:
    role = REGISTRY[role_id]
    if not job_id or not idempotency_key or not input_revision or not expires_at:
        raise ValueError('Complete job identity and revision required')
    if consent_scope != role.consent_scope:
        raise ValueError('Role consent scope mismatch')
    ceiling = role.max_cost_rub if store is None else str(effective_job_ceiling(role_id, store, now=now))
    return JobBudgetContract(job_id, role_id, role.purpose, role.model, ceiling,
                             role.max_turns, idempotency_key, input_revision,
                             expires_at, role.consent_scope)


def skip_reason(*, consent: bool, fresh_data: bool, budget_rub: Decimal, ceiling_rub: Decimal,
                already_done: bool = False, expired: bool = False) -> str | None:
    if not consent:
        return 'owner_consent_required'
    if expired:
        return 'expired'
    if already_done:
        return 'already_done'
    if not fresh_data:
        return 'no_new_data'
    if budget_rub <= 0 or budget_rub < ceiling_rub:
        return 'insufficient_budget'
    return None


def telemetry(store: BudgetStore, *, now: datetime | None = None) -> dict[str, Any]:
    report = store.global_report(now=now or datetime.now(UTC))
    if not report.get('purpose_quotas'):
        store.enable_purpose_limits()
        report = store.global_report(now=now or datetime.now(UTC))
    roles = {}
    for role_id in REGISTRY:
        roles[role_id] = role_budget(role_id, store, now=now)
    return {'global': {key: report[key] for key in
                       ('settled_month', 'unresolved_reserves', 'remaining_rub',
                        'hard_monthly_limit_rub', 'states', 'by_model_rub', 'by_task_rub')},
            'purposes': report['purpose_quotas'], 'roles': roles}


def require_job_budget(role_id: str, store: BudgetStore, *, spent_rub: Decimal,
                       now: datetime | None = None) -> Decimal:
    remaining = effective_job_ceiling(role_id, store, now=now) - spent_rub
    if remaining <= 0:
        raise BudgetReservationDenied('Job cost ceiling exhausted; no dispatch')
    return remaining
