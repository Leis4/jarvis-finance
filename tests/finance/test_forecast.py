"""Verify the legacy entry point cannot bypass audited forecast coverage."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from jarvis.finance.db import FinanceDB
from jarvis.finance.domain import Transaction
from jarvis.finance.forecast import compute_forecast, forecaster, render_forecast
from jarvis.finance.notifications.triggers import check_forecast_negative

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> FinanceDB:
    result = FinanceDB(tmp_path / "isolated.sqlite")
    result.init()
    return result


def confirmed_fixture(db: FinanceDB, closing: str = "4000") -> None:
    end = NOW.date()
    start = end - timedelta(days=29)
    db.set_setting("expected_accounts", ["main"])
    db.set_setting("account_coverage", {"main": {"start": start.isoformat(), "end": end.isoformat(), "complete": True, "source": "fixture"}})
    db.set_setting("source_health:fixture", {"status": "ok", "checked_at": NOW.isoformat(),
                                            "last_success_at": NOW.isoformat(), "coverage_end": end.isoformat()})
    transactions = []
    for offset in range(30):
        day = start + timedelta(days=offset)
        for category, amount in [("food", "-100"), ("salary", "50")]:
            transactions.append(Transaction(day, Decimal(amount), "RUB", "isolated fixture", "fixture", category,
                                            "main", f"fixture:{day}:{category}"))
    assert db.add_transactions(transactions, source="fixture") == 60
    assert db.add_transactions(transactions, source="fixture") == 0
    db.record_account_balance("main", "RUB", start - timedelta(days=1), Decimal(closing) + 1500, "fixture")
    db.record_account_balance("main", "RUB", end, Decimal(closing), "fixture")


def test_numeric_refusal_survives_fresh_complete_adapter(db: FinanceDB, monkeypatch: pytest.MonkeyPatch) -> None:
    confirmed_fixture(db)
    monkeypatch.setattr(forecaster, "build_audit_report", lambda *args, **kwargs: {
        "status": "complete", "sources": ["fixture"], "accounts": [],
        "forecast": {"status": "unavailable", "reason": "nonfinite_forecast_result"},
    })
    before = db.path.read_bytes()
    result = compute_forecast(db, now=NOW)
    assert result.status == "unavailable" and result.reason == "nonfinite_forecast_result"
    assert result.projected_balance_in_14d is None and result.monte_carlo_p10 is None
    assert db.path.read_bytes() == before


def test_empty_and_balance_snapshot_do_not_invent_zero_history(db: FinanceDB) -> None:
    db.add_budget_snapshot(NOW.date(), Decimal(5000), Decimal(0), Decimal(0))
    before = db.path.read_bytes()
    result = compute_forecast(db, now=NOW)
    assert result.status == "insufficient_data" and result.projected_balance_in_14d is None
    assert result.daily_avg is None and result.prob_solvent is None and result.used_model is None
    assert "insufficient_data" in render_forecast(result) and db.path.read_bytes() == before


@pytest.mark.parametrize("missing", ["inventory", "coverage", "balance", "lineage", "pending", "extra_account"])
def test_incomplete_inputs_never_emit_forecast_or_probability(db: FinanceDB, missing: str) -> None:
    confirmed_fixture(db)
    if missing == "inventory":
        db.set_setting("expected_accounts", [])
    elif missing == "coverage":
        db.set_setting("account_coverage", {})
    elif missing == "extra_account":
        db.set_setting("expected_accounts", ["main", "unknown"])
    else:
        with db.connect() as conn:
            if missing == "balance":
                conn.execute("DELETE FROM account_balance_observations")
            elif missing == "lineage":
                conn.execute("DELETE FROM source_events")
            else:
                conn.execute("UPDATE source_events SET status='pending_review' WHERE id=1")
            conn.commit()
    result = compute_forecast(db, now=NOW)
    assert result.status == "insufficient_data" and result.projected_balance_in_14d is None
    assert result.monte_carlo_p10 is None and result.prob_solvent is None


@pytest.mark.parametrize("source,status", [(None, "insufficient_data"), ("stale", "stale"),
                                         ("future", "stale"), ("failed", "unavailable")])
def test_source_failure_cannot_reuse_good_coverage(db: FinanceDB, source: str | None, status: str) -> None:
    confirmed_fixture(db)
    state = db.get_setting("source_health:fixture")
    if source is None:
        state = {}
    elif source == "stale":
        state["last_success_at"] = (NOW - timedelta(days=2)).isoformat()
    elif source == "future":
        state["last_success_at"] = (NOW + timedelta(days=2)).isoformat()
    else:
        state["status"] = "unavailable"
    db.set_setting("source_health:fixture", state)
    result = compute_forecast(db, now=NOW)
    assert result.status == status and result.projected_balance_in_14d is None


def test_reconciled_fresh_fixture_reuses_chronological_baseline_and_requested_horizon(db: FinanceDB) -> None:
    confirmed_fixture(db)
    before = db.path.read_bytes()
    result = compute_forecast(db, 7, now=NOW)
    assert result.status == "experimental" and result.projected_balance_in_14d == 3650
    assert result.used_model == "trailing_7"
    assert result.evidence["forecast"]["horizon_days"] == 7
    assert result.evidence["forecast"]["evaluation"].startswith("rolling origin")
    assert result.prob_solvent is None and result.monte_carlo_p50 is None and result.kalman_trend is None
    assert "Bootstrap p10/p90" in render_forecast(result) and db.path.read_bytes() == before
    assert result.to_dict()["status"] == "experimental"


def test_notification_refuses_unknown_and_marks_research_without_fake_probability(db: FinanceDB, monkeypatch: pytest.MonkeyPatch) -> None:
    original = compute_forecast
    monkeypatch.setattr(forecaster, "compute_forecast", lambda database, horizon_days: original(database, horizon_days, now=NOW))
    assert check_forecast_negative(str(db.path)) is None
    confirmed_fixture(db, "500")
    event = check_forecast_negative(str(db.path))
    assert event is not None and event.data["status"] == "experimental"
    assert 'Research' in event.title and 'P(' not in event.message
    assert "days_to_zero" not in event.data


@pytest.mark.parametrize("horizon", [0, -1, 366])
def test_resource_bounds(db: FinanceDB, horizon: int) -> None:
    with pytest.raises(ValueError, match="horizon_days"):
        compute_forecast(db, horizon, now=NOW)
