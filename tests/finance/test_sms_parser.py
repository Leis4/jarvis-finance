"""Tests for self-learning SMS parser."""
import sqlite3

from jarvis.finance.db import FinanceDB
from jarvis.finance.sms.ingest import ingest_sms
from jarvis.finance.sms.parser import (
    ParsedSMS,
    _parse_amount,
    classify_direction,
    parse_sms,
    try_regex_parse,
)


def test_parse_amount_with_spaces():
    assert _parse_amount("13 305.4") == 13305.4
    assert _parse_amount("1 000 000") == 1000000.0


def test_parse_amount_with_comma():
    assert _parse_amount("10,5") == 10.5
    assert _parse_amount("13 305,4") == 13305.4


def test_classify_direction_income():
    assert classify_direction("Зачисление") == "income"
    assert classify_direction("пополнение") == "income"


def test_classify_direction_expense():
    assert classify_direction("Оплата") == "expense"
    assert classify_direction("Снятие") == "expense"
    assert classify_direction("Покупка") == "expense"


def test_ingest_creates_transaction(tmp_path):
    db_file = tmp_path / "test.db"
    db = FinanceDB(db_file)
    db.init()

    parsed = ParsedSMS(
        account="3295",
        time_str="16:49",
        op_type="Оплата",
        amount=10.0,
        currency="р",
        merchant="Альфа-банк",
        balance=13305.4,
        direction="expense",
        raw_text="СЧЁТ3295 16:49\nОплата 10р Альфа-банк Баланс:\n13 305.4р",
    )

    res = ingest_sms(parsed, str(db_file))
    assert res["inserted"] is True
    assert res.get("tx_id") is not None

    conn = sqlite3.connect(str(db_file))
    row = conn.execute("SELECT amount, description, category FROM transactions WHERE id = ?", (res["tx_id"],)).fetchone()
    conn.close()

    assert row is not None
    assert float(row[0]) == -10.0
    assert "Оплата Альфа-банк" in row[1]
    assert row[2] == "uncategorized"


def test_ingest_dedup_prevents_double(tmp_path):
    db_file = tmp_path / "test.db"
    db = FinanceDB(db_file)
    db.init()

    parsed = ParsedSMS(
        account="3295",
        time_str="16:49",
        op_type="Оплата",
        amount=50.0,
        currency="р",
        merchant="OZON",
        balance=12000.0,
        direction="expense",
        raw_text="dummy",
    )

    res1 = ingest_sms(parsed, str(db_file))
    assert res1["inserted"] is True

    res2 = ingest_sms(parsed, str(db_file))
    assert res2["inserted"] is False
    assert res2.get("reason") == "duplicate"
    assert res2.get("tx_id") == res1.get("tx_id")


def test_try_regex_parse_matches_saved(tmp_path):
    db_file = tmp_path / "test.db"
    db = FinanceDB(db_file)
    db.init()

    pattern = (
        r"СЧЁТ(?P<account>\d{4})\s+(?P<time>\d{2}:\d{2})\s+"
        r"(?P<op_type>Оплата|Перевод|Зачисление|Снятие)\s+"
        r"(?P<amount>[\d\s.,]+)р\s+(?P<merchant>.+?)\s+Баланс:?\s*(?P<balance>[\d\s.,]+)р"
    )

    conn = sqlite3.connect(str(db_file))
    conn.execute("""
        INSERT INTO sms_patterns (regex_pattern, example_text, match_count, created_at)
        VALUES (?, 'example', 0, '2026-09-14T00:00:00Z')
    """, (pattern,))
    conn.commit()
    conn.close()

    sms_text = "СЧЁТ3295 16:49\nОплата 10р Альфа-банк Баланс:\n13 305.4р"
    parsed = try_regex_parse(sms_text, str(db_file))

    assert parsed is not None
    assert parsed.account == "3295"
    assert parsed.time_str == "16:49"
    assert parsed.op_type == "Оплата"
    assert parsed.amount == 10.0
    assert parsed.merchant == "Альфа-банк"
    assert parsed.balance == 13305.4
    assert parsed.direction == "expense"

    # Verify match_count was incremented
    conn = sqlite3.connect(str(db_file))
    row = conn.execute("SELECT match_count FROM sms_patterns WHERE regex_pattern = ?", (pattern,)).fetchone()
    conn.close()
    assert row is not None and row[0] == 1


def test_no_regex_returns_none(tmp_path):
    db_file = tmp_path / "test.db"
    db = FinanceDB(db_file)
    db.init()

    parsed = try_regex_parse("Неизвестное SMS сообщение", str(db_file))
    assert parsed is None


def test_parse_sber_opayment_alfa():
    sms = "СЧЁТ3295 14:23 Оплата 10р Альфа-банк Баланс: 13 270.4р"
    result = parse_sms(sms)
    assert result is not None
    assert result["account"] == "3295"
    assert result["amount"] == 10.0
    assert result["direction"] == "expense"
    assert result["merchant"] == "Альфа-банк"
    assert result["balance"] == 13270.4
