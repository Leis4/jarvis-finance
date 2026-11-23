from typing import Any

from jarvis.finance.reconciliation_triage import (
    extract_card_dates,
    normalize_text,
    triage_operations,
)


def target(**changes: Any) -> dict[str, Any]:
    row = {"tx_date": "2026-10-01", "amount": "-10", "currency": "RUB", "account": "bank",
           "description": "SHOP", "merchant": "SHOP"}
    row.update(changes)
    return {"operation": row, "local_account": "local", "processing_date": "2026-10-02"}


def db(**changes: Any) -> dict[str, Any]:
    row = {"id": 1, "tx_date": "2026-10-01", "amount": "-10.00", "currency": "RUB",
           "account": "sber", "merchant": "SHOP", "description": "SHOP", "external_id": "native"}
    row.update(changes)
    return row


def test_only_proven_text_formats_are_normalized() -> None:
    assert normalize_text(" Перевод от SHOP. Операция по карте ****0090") == "shop"
    assert normalize_text("•• 0090") == normalize_text("****0090")
    assert normalize_text("Alfa-Bank") != normalize_text("Альфа-Банк")
    assert normalize_text("Пётр Г.") != normalize_text("Г. Пётр")


def test_operation_processing_dates_and_auth_are_preserved() -> None:
    text = "01.10.2026 12:30 Покупка 10,00 20,00\n02.10.2026 123456 SHOP\n"
    dates = extract_card_dates(text)
    assert dates[0]["operation_date"] == "2026-10-01"
    assert dates[0]["processing_date"] == "2026-10-02"
    assert dates[0]["authorization_code"] == "123456"
    assert extract_card_dates("01.10.2026 12:30 Покупка 10,00\n")[0]["processing_date"] is None


def test_processing_date_is_observed_evidence_not_guessed_offset() -> None:
    report = triage_operations([target()], [db(tx_date="2026-10-02")])
    c = report["items"][0]["candidates"][0]
    assert report["classification_counts"] == {"exact/high-confidence": 1}
    assert c["processing_date_equal"] and c["date_offset_proven"]
    assert not report["items"][0]["apply_allowed"]


def test_shared_amount_with_no_date_or_text_does_not_make_candidate() -> None:
    report = triage_operations([target()], [db(tx_date="2026-10-08", merchant="OTHER", description="OTHER")])
    assert report["classification_counts"] == {"no candidate": 1}


def test_opposite_sign_transfer_is_review_not_normalized_match() -> None:
    report = triage_operations([target(description="Перевод от SHOP")], [db(amount="10")])
    assert report["classification_counts"] == {"ambiguous": 1}
    c = report["items"][0]["candidates"][0]
    assert c["transfer_opposite_sign"] and not c["signed_amount_equal"]


def test_duplicates_and_reused_db_rows_are_ambiguous() -> None:
    assert triage_operations([target()], [db(), db(id=2)])["classification_counts"] == {"ambiguous": 1}
    assert triage_operations([target(), target()], [db()])["classification_counts"] == {"ambiguous": 2}


def test_unproven_offset_currency_and_text_stay_pending() -> None:
    assert triage_operations([target()], [db(tx_date="2026-10-03")])["classification_counts"] == {"likely": 1}
    assert triage_operations([target()], [db(currency="USD")])["classification_counts"] == {"ambiguous": 1}
    assert triage_operations([target()], [db(merchant="OTHER", description="OTHER")])["classification_counts"] == {"ambiguous": 1}
