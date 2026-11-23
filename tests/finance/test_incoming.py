"""Tests for incoming transfer classification module."""
import sqlite3
from datetime import UTC, datetime

import pytest

from jarvis.finance.db import FinanceDB
from jarvis.finance.incoming.classifier import (
    _amount_matches,
    _bank_matches,
    _extract_sender,
    _parse_typical_amounts,
    classify_incoming,
    clean_known_sender_duplicates,
    clean_non_incoming,
    fix_pending_high_confidence,
    process_all_unclassified_incoming,
    reset_parental_classifications,
    save_classification,
)
from jarvis.finance.incoming.learner import (
    confirm_classification,
    reject_classification,
)


def now_iso():
    return datetime.now(UTC).isoformat()


@pytest.fixture
def test_db(tmp_path):
    db_file = str(tmp_path / "test_incoming.db")
    db = FinanceDB(db_file)
    db.init()
    return db_file


def test_known_senders_seeded(test_db):
    conn = sqlite3.connect(test_db)
    rows = conn.execute("SELECT sender_pattern, transfer_type FROM known_senders").fetchall()
    conn.close()
    patterns = [r[0] for r in rows]
    assert "Николай Смирнов" in patterns
    assert "Ольга Смирнова" in patterns


def test_extract_sender():
    assert _extract_sender("Перевод от Николай Смирнов") == "Николай Смирнов"
    assert _extract_sender("Перевод от Ольга Смирнова на продукты") == "Ольга Смирнова"
    assert _extract_sender("Оплата товаров") == ""


def test_classify_parent_rule(test_db):
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (today, 10000.0, "income", "Перевод от Николай Смирнов", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is not None
    assert res["transfer_type"] in ("family_income", "parental_support")
    assert res["confidence"] == 1.0


def test_classify_parent_reverse_name_rule(test_db):
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (today, 8000.0, "income", "Перевод от Смирнова Ольга", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is not None
    assert res["transfer_type"] in ("family_income", "parental_support")
    assert res["confidence"] == 1.0


def test_classify_gift_rule(test_db):
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (today, 3000.0, "income", "Перевод от Бабушка с др", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is not None
    assert res["transfer_type"] == "gift"
    assert res["confidence"] == 0.9


def test_classify_stipend_rule(test_db):
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (today, 6000.0, "stipend", "Стипендия", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is not None
    assert res["transfer_type"] == "stipend"
    assert res["confidence"] == 1.0


def test_save_classification(test_db):
    classification = {
        "income_tx_id": 99,
        "transfer_type": "parental_support",
        "matched_expense_tx_id": None,
        "target_category": "food",
        "confidence": 1.0,
        "reasoning": "rule",
    }
    save_classification(test_db, classification)

    conn = sqlite3.connect(test_db)
    row = conn.execute("SELECT transfer_type, status FROM incoming_classifications WHERE income_tx_id = 99").fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "parental_support"
    assert row[1] == "auto_matched"


def test_fix_pending_high_confidence(test_db):
    conn = sqlite3.connect(test_db)
    now = now_iso()
    conn.execute("""
        INSERT INTO incoming_classifications (income_tx_id, transfer_type, confidence, status, created_at)
        VALUES (88, 'parental_support', 0.95, 'pending', ?)
    """, (now,))
    conn.commit()
    conn.close()

    fixed = fix_pending_high_confidence(test_db)
    assert fixed == 1

    conn = sqlite3.connect(test_db)
    status = conn.execute("SELECT status FROM incoming_classifications WHERE income_tx_id = 88").fetchone()[0]
    conn.close()
    assert status == "auto_matched"


def test_process_all_unclassified_incoming(test_db):
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    conn.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                 (today, 5000.0, "income", "Перевод от Николай Смирнов", now_iso()))
    conn.commit()
    conn.close()

    count = process_all_unclassified_incoming(test_db)
    assert count == 1


def test_confirm_classification(test_db):
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (today, 1500.0, "income", "Перевод от Иван Иванов за интернет", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    classification = {
        "income_tx_id": tx_id,
        "transfer_type": "compensation",
        "confidence": 0.7,
    }
    save_classification(test_db, classification)

    confirm_classification(test_db, tx_id, "compensation", target_category="bills")

    conn = sqlite3.connect(test_db)
    c_row = conn.execute("SELECT status, transfer_type FROM incoming_classifications WHERE income_tx_id = ?", (tx_id,)).fetchone()
    p_row = conn.execute("SELECT sender_pattern, transfer_type FROM transfer_patterns WHERE sender_pattern = ?", ("Иван Иванов",)).fetchone()
    conn.close()

    assert c_row[0] == "confirmed"
    assert p_row is not None
    assert p_row[1] == "compensation"


def test_reject_classification(test_db):
    classification = {
        "income_tx_id": 101,
        "transfer_type": "compensation",
        "confidence": 0.5,
    }
    save_classification(test_db, classification)
    reject_classification(test_db, 101)

    conn = sqlite3.connect(test_db)
    row = conn.execute("SELECT status FROM incoming_classifications WHERE income_tx_id = 101").fetchone()
    conn.close()

    assert row[0] == "rejected"


def test_bank_hint_amount_matching(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (today, 13000.0, "income", "Перевод из T-Bank. Операция по счету ****1688", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is not None
    assert res["transfer_type"] == "parental_support"
    assert res["confidence"] >= 0.85


def test_account_hint_removed(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (today, 150.0, "income", "Перевод из T-Bank. Операция по счету ****1688", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is None or res.get("transfer_type") != "parental_support"


def test_parental_requires_exact_amount(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (today, 150.0, "income", "Перевод из T-Bank", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is None or res.get("transfer_type") != "parental_support"


def test_parental_bank_and_amount(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (today, 13000.0, "income", "Перевод из T-Bank", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is not None
    assert res["transfer_type"] == "parental_support"
    assert res["confidence"] >= 0.85


def test_reset_parental_classifications(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    conn.execute("""
        INSERT INTO incoming_classifications (income_tx_id, transfer_type, confidence, status, created_at)
        VALUES (1, 'parental_support', 0.95, 'auto_matched', ?)
    """, (now_iso(),))
    conn.commit()
    conn.close()

    reset_count = reset_parental_classifications(test_db)
    assert reset_count == 1


def test_compensation_keyword_matching(test_db):
    conn = sqlite3.connect(test_db)
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (today, 150.0, "income", "Перевод от К. Максим за такси", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is not None
    assert res["transfer_type"] == "compensation"


def test_parse_typical_amounts_json():
    assert _parse_typical_amounts('[12800.0, 13000.0, 26000.0]') == [12800.0, 13000.0, 26000.0]
    assert _parse_typical_amounts([5000, 10000]) == [5000.0, 10000.0]


def test_parse_typical_amounts_csv():
    assert _parse_typical_amounts('12800, 13000, 26000') == [12800.0, 13000.0, 26000.0]


def test_parse_typical_amounts_invalid():
    assert _parse_typical_amounts(None) == []
    assert _parse_typical_amounts('invalid, json') == []


def test_bank_matches_tbank():
    assert _bank_matches("Перевод из T-Bank", "T-Bank") is True
    assert _bank_matches("Перевод из тинёк", "tbank") is True
    assert _bank_matches("Перевод от Иван", "T-Bank") is False


def test_bank_matches_sbol():
    assert _bank_matches("SBOL перевод", "SBOL") is True
    assert _bank_matches("Перевод из сбербанк", "sbol") is True


def test_amount_matches_tolerance():
    assert _amount_matches(13000.0, [13000.0]) is True
    assert _amount_matches(13200.0, [13000.0], tolerance=0.05) is True
    assert _amount_matches(15000.0, [13000.0], tolerance=0.05) is False


def test_self_transfer_ignored(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (datetime.now(UTC).date().isoformat(), 5000.0, "income", "Перевод от Г. Пётр Николайович. Операция по счету ****", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is None


def test_atm_ignored(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (datetime.now(UTC).date().isoformat(), 10000.0, "income", "ATM 60204482 KAZAN RUS", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is None


def test_vklad_karta_ignored(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (datetime.now(UTC).date().isoformat(), 1000.0, "income", "SBERBANK ONL@IN VKLAD-KARTA", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is None


def test_clean_non_incoming(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (datetime.now(UTC).date().isoformat(), 5000.0, "income", "ATM 60204482 KAZAN RUS", now_iso()))
    tx_id = cursor.lastrowid
    cursor.execute("INSERT INTO incoming_classifications (income_tx_id, transfer_type, confidence, status, created_at) VALUES (?, 'unknown', 0.5, 'pending', ?)",
                   (tx_id, now_iso()))
    conn.commit()
    conn.close()

    n = clean_non_incoming(test_db)
    assert n == 1


def test_clean_known_sender_duplicates(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    conn.execute("INSERT INTO known_senders (sender_pattern, transfer_type, created_at, last_seen_at) VALUES ('Father_Test_Dup', 'parental_support', ?, ?)",
                 (now_iso(), now_iso()))
    conn.commit()
    conn.close()

    n = clean_known_sender_duplicates(test_db)
    assert n >= 1


def test_family_seed_inserts_9_patterns(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    count = conn.execute("SELECT COUNT(1) FROM known_senders WHERE note = 'family_seed'").fetchone()[0]
    conn.close()
    assert count == 9


def test_name_match_azat_vakivovich(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (datetime.now(UTC).date().isoformat(), 13000.0, "income", "Перевод от Г. Николай Николаевич. Операция по счету ****", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is not None
    assert res["transfer_type"] == "parental_support"
    assert res["confidence"] == 0.98


def test_name_match_aleksandr(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (datetime.now(UTC).date().isoformat(), 1500.0, "income", "Перевод от М. Александр Александрович", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is not None
    assert res["transfer_type"] == "compensation"
    assert res["confidence"] == 0.98


def test_name_priority_over_bank_amount(test_db):
    db = FinanceDB(test_db)
    db.init()
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (tx_date, amount, category, description, created_at) VALUES (?, ?, ?, ?, ?)",
                   (datetime.now(UTC).date().isoformat(), 5000.0, "income", "Перевод от Г. Дамира Сагитовна", now_iso()))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = classify_incoming(test_db, tx_id)
    assert res is not None
    assert res["transfer_type"] == "gift"
    assert res["confidence"] == 0.98
