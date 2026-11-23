"""Bounded synthetic properties of existing pure financial contracts."""

from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from jarvis.finance.ledger import cashflow, fingerprint, finite_money

amounts = st.decimals(min_value="-1000000", max_value="1000000", places=4,
                      allow_nan=False, allow_infinity=False)


@settings(max_examples=40, deadline=None, derandomize=True, database=None)
@given(amounts)
def test_finite_money_preserves_exact_decimal(value: Decimal) -> None:
    assert finite_money(str(value)) == value
    assert finite_money(value) == value


@settings(max_examples=40, deadline=None, derandomize=True, database=None)
@given(amounts, st.sampled_from(["food", "shopping", "uncategorized"]))
def test_fingerprint_ignores_classification_and_decimal_scale(
    amount: Decimal, category: str,
) -> None:
    payload: dict[str, Any] = {
        "tx_date": "2026-01-01", "amount": str(amount), "currency": "RUB",
        "account": "synthetic", "description": "fixture", "merchant": "fixture",
        "category": category,
    }
    altered = {**payload, "amount": format(amount, ".6f"), "category": "pending_review"}
    assert fingerprint(payload) == fingerprint(altered)
    assert len(fingerprint(payload)) == 64


@settings(max_examples=40, deadline=None, derandomize=True, database=None)
@given(st.lists(amounts, max_size=30))
def test_cashflow_conserves_synthetic_economic_movement(values: list[Decimal]) -> None:
    rows = [{"amount": str(value), "category": "food", "currency": "RUB"}
            for value in values]
    flow = cashflow(rows)
    assert flow["movement"] == sum(values, Decimal(0))
    assert flow["expenses"] == -sum((value for value in values if value < 0), Decimal(0))
    # An expense label cannot prove the economic role of an incoming payment.
    assert flow["income"] == flow["excluded_movement"] == 0
    assert flow["unresolved_movement"] == sum((value for value in values if value > 0), Decimal(0))
    assert flow["net"] + flow["excluded_movement"] + flow["unresolved_movement"] == flow["movement"]


@settings(max_examples=40, deadline=None, derandomize=True, database=None)
@given(st.lists(amounts, max_size=30))
def test_known_economic_roles_conserve_net_movement(values: list[Decimal]) -> None:
    rows = [{"amount": str(value), "category": "salary" if value > 0 else "food", "currency": "RUB"}
            for value in values]
    flow = cashflow(rows)
    assert flow["net"] == flow["movement"] == sum(values, Decimal(0))
    assert flow["income"] == sum((value for value in values if value > 0), Decimal(0))
    assert flow["expenses"] == -sum((value for value in values if value < 0), Decimal(0))
    assert flow["excluded_movement"] == flow["unresolved_movement"] == 0
