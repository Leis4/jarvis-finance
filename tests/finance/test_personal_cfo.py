"""Personal CFO acceptance, using isolated synthetic bank evidence only."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.finance.db import FinanceDB
from jarvis.finance.domain import Transaction
from jarvis.finance.personal_cfo import PersonalCFO, PersonalPlan, backup_database
from jarvis.finance.personal_ui import question
from jarvis.finance.review_store import ReviewStore
from jarvis.finance.review_ui import create_review_app

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


@pytest.fixture
def cfo(tmp_path: Path) -> PersonalCFO:
    db = FinanceDB(tmp_path / "finance.sqlite")
    db.init()
    return PersonalCFO(db)


def capture() -> dict[str, Any]:
    return {"status": "captured", "products": {"accounts": [{"id": "a", "operation_resource": "account:a",
        "balance": {"amount": "12500.00", "currency": "RUB"}}], "cards": [{"id": "card", "account_id": "a"}]},
        "history": {"coverage": "complete", "scope": {"start": "2026-08-11", "end": "2026-11-09"},
        "pages": [{"request": {"resource": "account:a"}, "response": {"operations": [{
            "id": "op", "date": "2026-11-08T12:00:00+03:00", "is_financial": True,
            "amount": {"amount": "-900.00", "currency": "RUB"}, "merchant": "fixture-store", "description": "fixture"}]}}]}}


def test_purchase_requires_actual_bank_and_explicit_assumptions(cfo: PersonalCFO) -> None:
    result = question(cfo, "У меня 12 500 ₽ на карте, около 6–7 тысяч наличными. Стоит ли купить память за 11 800 ₽?")
    assert result["status"] == "insufficient_data"
    assert result["after_purchase_low"] is None  # owner statement is not a bank read
    assert cfo.plan().cash_low == "0.00"  # chat never commits unconfirmed profile input
    assert cfo.plan().cash_high == "0.00"
    assert "свежие банковские остатки" in result["missing"]
    assert all(row["amount"] is None for row in cfo.dashboard()["forecast"])


def test_exact_interval_stale_and_zero_obligations(cfo: PersonalCFO) -> None:
    cfo.ingest(capture(), now=NOW)
    cfo.save_plan(PersonalPlan(cash_low="6000", cash_high="7000", reserve="5000", obligations="2000",
        next_income_date=date(2026, 11, 16)))
    result = cfo.purchase("11800", now=NOW)
    assert result["after_purchase_low"] == "6700.00"
    assert result["after_purchase_high"] == "7700.00"
    assert result["status"] == "defer"
    assert result["after_protection_and_baseline"] == "-370.00"
    assert cfo.purchase("11800", now=NOW + timedelta(days=2))["status"] == "insufficient_data"


def test_repeat_does_not_duplicate_or_overwrite_correction(cfo: PersonalCFO) -> None:
    assert cfo.ingest(capture(), now=NOW)["inserted"] == 1
    tx = cfo.db.all_transactions()[0]
    assert cfo.correct(tx["id"], "food")
    assert cfo.ingest(capture(), now=NOW + timedelta(days=2))["inserted"] == 0
    assert cfo.dashboard(now=NOW + timedelta(days=2))["bank_fresh"] is False
    assert len(cfo.db.all_transactions()) == 1
    assert cfo.db.all_transactions()[0]["category"] == "food"
    assert cfo.learned_category("fixture-store") == "food"
    assert cfo.category("fixture-store", "changed description") == "food"


def test_conflicting_manual_labels_do_not_create_a_rule(cfo: PersonalCFO) -> None:
    for i, category in enumerate(("food", "transport")):
        tx_id = cfo.db.add_transaction(Transaction(NOW.date(), Decimal(-i - 1), "RUB", str(i), "mixed", "pending_review"))
        cfo.correct(tx_id, category)
    assert cfo.learned_category("mixed") is None


def test_exact_bound_card_representation_is_resolved_without_second_insert(cfo: PersonalCFO) -> None:
    data = capture()
    duplicate = {**data["history"]["pages"][0], "request": {"resource": "card:card"}}
    data["history"]["pages"].append(duplicate)
    result = cfo.ingest(data, now=NOW)
    assert (result["inserted"], result["held"]) == (1, 0)
    assert cfo.db.get_setting("cfo.normalization_receipt")["counts"]["duplicate_representation"] == 1
    assert cfo.dashboard(now=NOW)["bank_rub"] == "12500.00"
    assert cfo.dashboard(now=NOW)["bank"]["bank_reconciled"] is False


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0.001", "1e99"])
def test_bad_owner_money(value: str) -> None:
    with pytest.raises(ValueError):
        PersonalPlan(cash_low=value)


def test_backup_preserves_original_and_is_valid(cfo: PersonalCFO, tmp_path: Path) -> None:
    cfo.db.add_transaction(Transaction(NOW.date(), Decimal("1.23"), "RUB", "fixture", "fixture", "salary"))
    backup = backup_database(cfo.db, tmp_path / "backups")
    restored = FinanceDB(backup)
    assert restored.all_transactions() == cfo.db.all_transactions()


def test_future_capture_rejected_before_any_insert(cfo: PersonalCFO) -> None:
    packet = capture()
    packet["history"]["scope"]["end"] = "2027-01-01"
    with pytest.raises(ValueError):
        cfo.ingest(packet, now=NOW)
    assert cfo.db.all_transactions() == []


def test_recurrence_is_candidate_not_future_income(cfo: PersonalCFO) -> None:
    for day in (date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)):
        cfo.db.add_transaction(Transaction(day, Decimal("1000.00"), "RUB", "fixture", "fixture", "salary"))
    data = cfo.dashboard(now=NOW)
    candidate = data["patterns"][0]
    assert candidate["kind"] == "income_candidate"
    assert candidate["typical_net_expense"] == "1000.00"
    assert candidate["included_in_forecast"] is False
    assert cfo.plan().next_income_date is None


def test_failed_restore_cooldown(cfo: PersonalCFO, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.finance import personal_bank
    calls = []

    def unavailable(start: date, end: date) -> dict[str, Any]:
        calls.append((start, end))
        return {"status": "auth_required", "code": "pin_required"}

    monkeypatch.setattr(personal_bank, "capture_bank", unavailable)
    cfo.ensure_bank_current()
    cfo.ensure_bank_current()
    assert len(calls) == 1


def test_web_boundary_and_saved_plan(cfo: PersonalCFO, tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "review")
    store.initialize({"source_kind": "pdf", "apply_allowed": False, "items": [], "groups": []})
    app = create_review_app(store, "t" * 32, personal_cfo=cfo)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001)) as client:
        assert client.get("/api/cfo").status_code == 403
        headers = {"x-review-token": "t" * 32}
        assert client.get("/api/cfo", headers=headers).status_code == 200
        assert client.put("/api/cfo/plan", headers=headers, json={"cash_low": "6000", "cash_high": "7000"}).status_code == 200
        result = client.post("/api/cfo/question", headers=headers,
            json={"message": "Могу ли купить ОЗУ за 11 800 ₽?"}).json()
        assert result["status"] == "insufficient_data"
        page = client.get("/", headers=headers).text
        assert 'data-page="dashboard"' in page and 'data-page="qwen"' in page
        assert "__SCRIPT__" not in page
