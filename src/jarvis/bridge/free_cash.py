from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from jarvis.finance.control import capacity_scenarios
from jarvis.finance.db import FinanceDB
from jarvis.finance.ledger import finite_money
from jarvis.finance.review_sources import accepted_capacity_inputs
from jarvis.finance.review_store import ReviewStore


class FreeCashUnavailable(RuntimeError):
    """Refuse investment capacity without an accepted source-bound adapter."""

    status = "insufficient_data"
    reason = "accepted_capacity_evidence_required"
    database_modified = False

    def __init__(self) -> None:
        super().__init__(f"{self.status}: {self.reason}")


def get_free_cash(db: FinanceDB, horizon_days: int = 30) -> float:
    """Keep legacy callers gated until balances and commitments are accepted."""
    if isinstance(horizon_days, bool) or horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    # Legacy snapshots lack complete coverage and dated accepted commitments.
    # Net movement is not a balance. Never open the DB or infer reserves here.
    raise FreeCashUnavailable


def should_invest(db: FinanceDB, min_free: float = 1000.0) -> bool:
    """Propagate unavailable capacity instead of treating it as zero or False."""
    return get_free_cash(db) >= min_free


def capacity_preview(
    store: ReviewStore, fact_ids: dict[str, str], *, start: date, end: date, currency: str,
    scenarios: dict[str, Decimal], now: datetime | None = None,
) -> dict[str, Any]:
    """Explain accepted financial scenarios without approving investment or goals."""
    evidence = accepted_capacity_inputs(store, fact_ids, start=start, end=end, currency=currency, now=now)
    if evidence["status"] != "accepted_evidence":
        return {**evidence, "scenarios": None, "investment_allowed": False}
    inputs = evidence["inputs"]
    result = capacity_scenarios(inputs["balances"], frozenset(inputs["expected_accounts"]), currency,
        Decimal(inputs["obligations"]), Decimal(inputs["reserve"]), scenarios,
        obligations_confirmed=True, reserve_confirmed=True,
        planning_source_sha256=inputs["planning_source_sha256"], now=now)
    return {**result, "period": evidence["period"], "provenance": evidence["provenance"],
        "accepted_evidence_sha256": inputs["planning_source_sha256"],
        "input_summary": {"liquid_balance": str(sum((finite_money(r["amount"]) for r in inputs["balances"]), Decimal(0))),
                          "obligations": inputs["obligations"], "reserve": inputs["reserve"], "currency": currency},
        "inclusion_rule": evidence["inclusion_rule"], "investment_allowed": False,
        "limitations": "Human-attested source facts; scenario only, no FX, income forecast or execution permission"}
