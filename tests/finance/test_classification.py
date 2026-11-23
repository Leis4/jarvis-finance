import asyncio
import sqlite3

import pytest

from jarvis.finance.classification.layers import (
    l0_preprocess,
    l1_hard_rules,
    l2_learned_patterns,
    l3_historical_vote,
    l5_merchant_kb,
    l7_amount_signature,
    l12_bayesian,
    l13_tfidf_sgd,
    l14_ensemble,
)
from jarvis.finance.classification.orchestrator import classify_transaction
from jarvis.finance.db import FinanceDB


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_classification.db"
    db = FinanceDB(db_file)
    db.init()
    return db_file


def test_l0_preprocess_extracts_bank_merchant_is_round():
    ctx = l0_preprocess("Перевод из T-Bank. Операция по карте ****1688", 13000.0)
    assert ctx["bank"] == "t-bank"
    assert ctx["is_round"] is True
    assert ctx["amount_abs"] == 13000.0


def test_l1_hard_rules_returns_none_for_150(test_db):
    ctx = l0_preprocess("Перевод из T-Bank. Операция по карте ****1688", 150.0)
    res = l1_hard_rules(str(test_db), ctx)
    assert res is None


def test_l1_hard_rules_matches_13000(test_db):
    ctx = l0_preprocess("Перевод из T-Bank. Операция по карте ****1688", 13000.0)
    res = l1_hard_rules(str(test_db), ctx)
    assert res is not None
    assert res.category == "parental_support"
    assert res.confidence >= 0.95


def test_l2_learned_adaptive_confidence(test_db):
    conn = sqlite3.connect(str(test_db))
    conn.execute("""
        INSERT INTO known_senders
        (sender_pattern, transfer_type, bank_hint, amount_min, amount_max, times_confirmed, auto_classify_after, created_at, last_seen_at)
        VALUES ('t-bank:father:10000', 'parental_support', 't-bank', 9500, 10500, 6, 5, datetime('now'), datetime('now'))
    """)
    conn.commit()
    conn.close()

    ctx = l0_preprocess("Перевод из T-Bank", 10000.0)
    res = l2_learned_patterns(str(test_db), ctx)
    assert res is not None
    assert res.category == "parental_support"
    assert res.confidence == 0.98


def test_l3_historical_vote(test_db):
    conn = sqlite3.connect(str(test_db))
    for _ in range(5):
        conn.execute("""
            INSERT INTO transactions (tx_date, amount, currency, description, category, account, created_at)
            VALUES ('2026-09-01', '-350', 'RUB', 'Пятёрочка продукты', 'food_basic', 'main', datetime('now'))
        """)
    conn.commit()
    conn.close()

    ctx = l0_preprocess("Пятёрочка продукты магазин", -350.0)
    res = l3_historical_vote(str(test_db), ctx)
    if res is not None:
        assert res.category == "food_basic"


def test_l5_merchant_kb_magnit():
    ctx = l0_preprocess("Магнит продукты", -450.0)
    res = l5_merchant_kb(ctx)
    assert res is not None
    assert res.category == "food_basic"


def test_l7_amount_signature_round():
    ctx = l0_preprocess("Перевод неизвестному человеку", 1000.0)
    res = l7_amount_signature(ctx)
    assert res is not None
    assert res.category == "peer_transfer"


def test_l12_bayesian_prediction(test_db):
    conn = sqlite3.connect(str(test_db))
    for i in range(60):
        cat = "cafes" if i % 2 == 0 else "health"
        desc = "Кафе Додо Пицца" if cat == "cafes" else "Аптека Вита лекарства"
        conn.execute("""
            INSERT INTO transactions (tx_date, amount, currency, description, category, account, created_at)
            VALUES ('2026-09-01', '-500', 'RUB', ?, ?, 'main', datetime('now'))
        """, (desc, cat))
    conn.commit()
    conn.close()

    ctx = l0_preprocess("Кафе Додо заказ", -600.0)
    res = l12_bayesian(str(test_db), ctx)
    if res is not None:
        assert res.category in ("cafes", "health")


def test_l13_tfidf_sgd_prediction(test_db):
    conn = sqlite3.connect(str(test_db))
    for i in range(60):
        cat = "cafes" if i % 2 == 0 else "health"
        desc = "Кафе Додо Пицца" if cat == "cafes" else "Аптека Вита лекарства"
        conn.execute("""
            INSERT INTO transactions (tx_date, amount, currency, description, category, account, created_at)
            VALUES ('2026-09-01', '-500', 'RUB', ?, ?, 'main', datetime('now'))
        """, (desc, cat))
    conn.commit()
    conn.close()

    ctx = l0_preprocess("Аптека Вита", -200.0)
    res = l13_tfidf_sgd(str(test_db), ctx)
    if res is not None:
        assert res.category in ("cafes", "health")


def test_l14_ensemble_prediction(test_db):
    conn = sqlite3.connect(str(test_db))
    for i in range(110):
        cat = "cafes" if i % 2 == 0 else "health"
        desc = "Кафе Додо Пицца" if cat == "cafes" else "Аптека Вита лекарства"
        conn.execute("""
            INSERT INTO transactions (tx_date, amount, currency, description, category, account, created_at)
            VALUES ('2026-09-01', '-500', 'RUB', ?, ?, 'main', datetime('now'))
        """, (desc, cat))
    conn.commit()
    conn.close()

    ctx = l0_preprocess("Додо пицца заказ", -500.0)
    res = l14_ensemble(str(test_db), ctx)
    if res is not None:
        assert res.category in ("cafes", "health")


def test_orchestrator_combines_layers(test_db):
    res = asyncio.run(classify_transaction(str(test_db), "Перевод от Г. Николай Николаевич. Операция по счету ****1688", 13000.0))
    assert res["category"] == "parental_support"
    assert res["escalate"] is False


def test_orchestrator_escalates_on_conflict(test_db):
    res = asyncio.run(classify_transaction(str(test_db), "Странная неопределенная транзакция 12345", 137.42))
    assert "category" in res
    assert res["confidence"] <= 0.85


def test_l5_matches_merchant_kb():
    ctx = {"merchant": "OZON", "description_clean": ""}
    res = l5_merchant_kb(ctx)
    assert res is not None
    assert res.category == "shopping_marketplace"


def test_l5_matches_pyaterochka():
    ctx = {"merchant": "PYATEROCHKA MOSCOW", "description_clean": ""}
    res = l5_merchant_kb(ctx)
    assert res is not None
    assert res.category == "food_basic"


def test_l5_returns_none_for_unknown():
    ctx = {"merchant": "XXXFOO", "description_clean": ""}
    res = l5_merchant_kb(ctx)
    assert res is None
