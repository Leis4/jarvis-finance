"""Tests for classification edge cases, person names, investment markers, and positive-only sanity checks."""
from __future__ import annotations

from jarvis.finance.scheduler.jobs import _detect_special_category


def test_negative_compensation_falls_back_to_peer_transfer_out() -> None:
    op = {"merchant": "Серафима Александровна М.", "amount": -1000.0}
    cat = _detect_special_category(op)
    assert cat == "peer_transfer_out"


def test_positive_compensation_stays() -> None:
    op = {"merchant": "Возврат за покупку", "description": "Компенсация", "amount": 500.0}
    # If compensation is detected manually or by rule on positive amount, it should stay compensation
    cat = _detect_special_category(op)
    assert cat != "peer_transfer_out"


def test_person_self_name_is_transfer() -> None:
    op = {"merchant": "Пётр Николайович Г.", "amount": -5000.0}
    assert _detect_special_category(op) == "pending_review"


def test_parents_positive_is_family_income() -> None:
    op = {"merchant": "Николай Николаевич Г.", "amount": 10000.0}
    assert _detect_special_category(op) == "family_income"


def test_parents_negative_is_family_income() -> None:
    op = {"merchant": "Николай Николаевич Г.", "amount": -5000.0}
    assert _detect_special_category(op) == "family_income"


def test_peer_positive_is_in() -> None:
    op = {"merchant": "Ярослав Вячеславович Т.", "amount": 1000.0}
    assert _detect_special_category(op) == "peer_transfer_in"


def test_peer_negative_is_out() -> None:
    op = {"merchant": "Кирилл Евгеньевич П.", "amount": -300.0}
    assert _detect_special_category(op) == "peer_transfer_out"


def test_friend_positive_is_compensation() -> None:
    op = {"merchant": "Александр Александрович М.", "amount": 1040.0}
    assert _detect_special_category(op) == "compensation"


def test_friend_negative_is_peer_out() -> None:
    op = {"merchant": "Александр Александрович М", "amount": -255.0}
    assert _detect_special_category(op) == "peer_transfer_out"


def test_split_only_for_split_merchant() -> None:
    op = {"merchant": "FITNES-TSENTR MOSCOW RUS", "amount": -2000.0, "description": ""}
    assert _detect_special_category(op) != "installment_yandex_split"


def test_split_requires_merchant_marker() -> None:
    op_fitnes = {"merchant": "FITNES-TSENTR MOSCOW RUS", "amount": -2000.0, "description": "x"}
    op_split = {"merchant": "Яндекс.Сплит", "amount": -2000.0, "description": "Оплата покупки"}
    assert _detect_special_category(op_fitnes) is None
    from jarvis.finance.classification.layers import l5_merchant_kb
    ctx_split = {"merchant": op_split["merchant"], "description_clean": op_split["description"]}
    res = l5_merchant_kb(ctx_split)
    assert res is not None and res.category == "installment_yandex_split"



def test_split_merchant_still_works() -> None:
    from jarvis.finance.ml_local.merchant_kb import extract_merchant_from_text
    assert extract_merchant_from_text("Yandex Split Payment") == "yandex split"





def test_investment_buy_marker() -> None:
    op = {"merchant": "Фонд Сберегательный", "description": "Покупка биржевых паев", "amount": -2395.64}
    assert _detect_special_category(op) == "investment_purchase"


def test_investment_transfer_marker() -> None:
    op = {"merchant": "RUS", "description": "Автопополнение брокерского счета", "amount": -2395.64}
    assert _detect_special_category(op) == "investment_principal"


def test_sber_fund_pair_both_investment() -> None:
    op35 = {"merchant": "RUS", "description": "Автопополнение брокерского счета", "amount": -2395.64}
    op36 = {"merchant": "Фонд Сберегательный", "description": "Покупка биржевых паев", "amount": -2395.64}
    assert _detect_special_category(op35) == "investment_principal"
    assert _detect_special_category(op36) == "investment_purchase"
