"""Synthetic savings documents prove money without inventing account ownership."""
from dataclasses import replace
from decimal import Decimal

import pytest

from jarvis.finance.bank.savings_validation import savings_category
from jarvis.finance.bank.sber_statement import parse_sber_pdf_text
from jarvis.finance.bank.statement_validation import validate_statement
from jarvis.finance.ledger import cashflow

TEXT = '''Выписка по вкладу или счёту
Сберегательный счет
Валюта Российский рубль
за период 01.01.2026 — 05.01.2026
Остаток на 01.01.2026 0,00
Остаток на 05.01.2026 80,01
05.01.2026 Списание
с/с 00000 000 0 0000 0000000
104, № 1-104 -20,00 80,01
04.01.2026 Капитализация вклада
с/с 00000 000 0 0000 0000000
103, № 1-103 +0,01 100,01
02.01.2026 Зачисление
с/с 00000 000 0 0000 0000000
102, № 1-102 +100,00 100,00
01.01.2026 Открытие счета
с/с 00000 000 0 0000 0000000
101, № 1-101 +0,00 0,00
'''


def test_signed_savings_rows_anchors_and_principal_roles() -> None:
    parsed = parse_sber_pdf_text(TEXT)
    assert len(parsed.transactions) == 3
    assert [t.amount for t in parsed.transactions] == [Decimal('-20.00'), Decimal('0.01'), Decimal('100.00')]
    result = validate_statement(TEXT, parsed.transactions, source_format='pdf')
    assert result['verdict'] == 'safe' and result['row_parity']
    assert result['savings_check']['non_monetary_events'] == 1
    assert not result['savings_check']['canonical_account_verified']
    assert result['savings_check']['coverage_verified']
    assert result['balance_check']['opening_balance_verified'] and result['balance_check']['closing_balance_verified']
    assert result['balance_check']['operation_direction_verified']
    flow = cashflow([{'amount': str(t.amount), 'currency': t.currency, 'category': t.category} for t in parsed.transactions])
    assert flow['income'] == Decimal('0.01') and flow['expenses'] == 0
    assert flow['unresolved_movement'] == Decimal('80.00')
    assert flow['movement'] == flow['net'] + flow['excluded_movement'] + flow['unresolved_movement']


@pytest.mark.parametrize('old,new', [('80,01','90,01'), ('+100,00','-100,00'), ('02.01.2026 Зачисление','31.12.2025 Зачисление')])
def test_corrupt_balance_sign_or_coverage_is_unsafe(old: str, new: str) -> None:
    text = TEXT.replace(old, new)
    result = validate_statement(text, parse_sber_pdf_text(text).transactions, source_format='pdf')
    assert result['verdict'] == 'unsafe' and not result['can_import']


def test_missing_anchor_is_review_and_parser_disagreement_is_unsafe() -> None:
    text = TEXT.replace('Остаток на 05.01.2026 80,01\n', '')
    parsed = parse_sber_pdf_text(text)
    result = validate_statement(text, parsed.transactions, source_format='pdf')
    assert result['verdict'] == 'review' and not result['balance_check']['closing_balance_verified']
    altered = [replace(t, amount=-t.amount) for t in parsed.transactions]
    assert validate_statement(text, altered, source_format='pdf')['verdict'] == 'unsafe'


def test_zero_payment_is_not_silently_dropped_as_account_metadata() -> None:
    text = TEXT.replace('Открытие счета', 'Списание')
    parsed = parse_sber_pdf_text(text)
    assert len(parsed.transactions) == 4
    assert validate_statement(text, parsed.transactions, source_format='pdf')['verdict'] == 'unsafe'


@pytest.mark.parametrize('description,amount', [('Зачисление','100'), ('Списание','-20'),
    ('Возврат процентов','5'), ('Капитализация вклада','-5')])
def test_unproven_principal_and_interest_reversal_stay_unknown(description: str, amount: str) -> None:
    assert savings_category(description, Decimal(amount)) == 'unknown'


def test_declared_window_cannot_be_invented_from_seen_transactions() -> None:
    text = TEXT.replace('за период 01.01.2026 — 05.01.2026\n', '')
    result = validate_statement(text, parse_sber_pdf_text(text).transactions, source_format='pdf')
    assert result['verdict'] == 'review' and not result['savings_check']['declared_window_verified']
    text = TEXT.replace('за период 01.01.2026 — 05.01.2026', 'за период 02.01.2026 — 05.01.2026')
    assert validate_statement(text, parse_sber_pdf_text(text).transactions, source_format='pdf')['verdict'] == 'unsafe'


def test_unparsed_savings_row_and_duplicate_reference_are_not_accepted() -> None:
    text = TEXT + '05.01.2026 Списание\nбез подписанной суммы и остатка\n'
    result = validate_statement(text, parse_sber_pdf_text(text).transactions, source_format='pdf')
    assert 'savings_unparsed_rows' in result['unsafe_reasons']
    duplicate = '05.01.2026 Списание\nс/с 00000 000 0 0000 0000000\n104, № 1-104 -20,00 80,01\n'
    text = TEXT + duplicate
    result = validate_statement(text, parse_sber_pdf_text(text).transactions, source_format='pdf')
    assert 'savings_duplicate_reference' in result['unsafe_reasons']


@pytest.mark.parametrize('description', ['Перевод в другой банк', 'Альфа-банк', 'SBERBANK ONL@IN KARTA-VKLAD'])
def test_pdf_transfer_text_does_not_prove_ownership(description: str) -> None:
    text = ('Выписка по счёту дебетовой карты\n'
        f'01.01.2026 10:00 Перевод +100,00 100,00\n01.01.2026 123456 {description}\n')
    parsed = parse_sber_pdf_text(text)
    assert len(parsed.transactions) == 1 and parsed.transactions[0].category == 'unknown'
    t = parsed.transactions[0]
    flow = cashflow([{'amount': str(t.amount), 'currency': t.currency, 'category': t.category}])
    assert flow['income'] == flow['expenses'] == flow['excluded_movement'] == 0
    assert flow['unresolved_movement'] == Decimal('100.00')
