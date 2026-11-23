"""Independently verify signed savings rows against document balance anchors."""
from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from jarvis.finance.domain import Transaction

_DATE = r'\d{2}\.\d{2}\.\d{4}'
_NUMBER = r'\d[\d \xa0]*[,.]\d{2}'
_ROW = re.compile(rf'^(?P<day>{_DATE})\s+(?P<kind>[^\d].*)$')
_TAIL = re.compile(rf'(?P<amount>[+\-−]\s*{_NUMBER})\s+(?P<balance>[\-−]?{_NUMBER})\s*$')
_ANCHOR = re.compile(rf'^Остаток\s+на\s+(?P<day>{_DATE})\s+(?P<amount>[\-−]?{_NUMBER})$', re.IGNORECASE)


def savings_document(text: str) -> bool:
    header = text[:6000].casefold().replace('ё', 'е')
    return 'сберегательный счет' in header or 'выписка по вкладу' in header


def zero_account_event(description: str, amount: Decimal) -> bool:
    return amount == 0 and bool(re.fullmatch(r'(?:открытие|закрытие)\s+сч[её]та', description.strip(), re.IGNORECASE))


def savings_category(description: str, amount: Decimal) -> str:
    """Principal credits/debits are not evidence of economic income/spending."""
    interest = re.fullmatch(r'капитализация(?:\s+вклада)?|начисленные\s*%|начисление\s+процентов|выплата\s+процентов', description.strip(), re.IGNORECASE)
    return 'interest' if amount > 0 and interest else 'unknown'


def _money(value: str) -> Decimal:
    return Decimal(value.replace(' ', '').replace('\xa0', '').replace('−', '-').replace(',', '.'))


def _day(value: str) -> str:
    return datetime.strptime(value, '%d.%m.%Y').replace(tzinfo=UTC).date().isoformat()


def validate_savings(text: str, transactions: list[Transaction]) -> dict[str, Any]:
    """Keep document arithmetic separate from canonical-account or economic proof."""
    lines = [' '.join(line.split()) for line in text.splitlines()]
    anchors = []
    rows: list[dict[str, Any]] = []
    pending: tuple[str, str] | None = None
    unparsed = 0
    for line in lines:
        anchor = _ANCHOR.fullmatch(line)
        if anchor:
            anchors.append((_day(anchor['day']), _money(anchor['amount'])))
            continue
        head = _ROW.fullmatch(line)
        tail = _TAIL.search(line)
        if head:
            if pending:
                unparsed += 1
            kind = head['kind'][:tail.start() - head.start('kind')].strip() if tail else head['kind']
            pending = (_day(head['day']), kind)
        if tail and pending:
            rows.append({'day': pending[0], 'kind': pending[1], 'amount': _money(tail['amount']),
                         'balance': _money(tail['balance']), 'reference': line[:tail.start()].strip()})
            pending = None
    unparsed += pending is not None
    financial = [r for r in rows if not zero_account_event(r['kind'], r['amount'])]
    source = Counter((r['day'], r['amount']) for r in financial)
    parsed = Counter((t.tx_date.isoformat(), t.amount) for t in transactions)
    parity = source == parsed
    valid_anchors = len(anchors) == 2 and anchors[0][0] <= anchors[1][0]
    period = re.search(rf'за\s+период\s+({_DATE})\s*[—–-]\s*({_DATE})', text, re.IGNORECASE)
    window = (_day(period[1]), _day(period[2])) if period else None
    window_matches = bool(valid_anchors and window == (anchors[0][0], anchors[1][0]))
    mismatches: list[int] = []
    if valid_anchors:
        for ordered in (rows, list(reversed(rows))):
            previous = anchors[0][1]
            bad = 0
            for row in ordered:
                bad += previous + row['amount'] != row['balance']
                previous = row['balance']
            bad += previous != anchors[1][1]
            mismatches.append(bad)
    mismatch = min(mismatches) if mismatches else 0
    coverage = valid_anchors and all(anchors[0][0] <= r['day'] <= anchors[1][0] for r in rows)
    total_matches = valid_anchors and anchors[0][1] + sum((r['amount'] for r in rows), Decimal(0)) == anchors[1][1]
    duplicates = sum(n - 1 for n in Counter((r['day'], r['amount'], r['reference']) for r in rows).values())
    reasons = []
    if not parity:
        reasons.append('savings_signed_row_mismatch')
    if unparsed:
        reasons.append('savings_unparsed_rows')
    if duplicates:
        reasons.append('savings_duplicate_reference')
    if mismatch or valid_anchors and not total_matches:
        reasons.append('savings_balance_mismatch')
    if valid_anchors and not coverage:
        reasons.append('savings_outside_coverage')
    if not re.search(r'российский\s+рубль|\bRUB\b', text[:6000], re.IGNORECASE) or any(t.currency != 'RUB' for t in transactions):
        reasons.append('savings_currency_unverified')
    if window and valid_anchors and not window_matches:
        reasons.append('savings_window_mismatch')
    complete = bool(rows) and valid_anchors and window_matches and coverage and total_matches and not reasons
    return {'financial_rows': len(financial), 'non_monetary_events': len(rows) - len(financial),
        'unparsed': unparsed, 'duplicates': duplicates, 'row_parity': parity,
        'checked': len(rows) if valid_anchors else 0, 'mismatch': mismatch,
        'opening_balance_verified': complete, 'closing_balance_verified': complete,
        'operation_direction_verified': parity and bool(rows) and not unparsed,
        'coverage_verified': complete, 'complete': complete, 'unsafe_reasons': reasons,
        'declared_window_verified': window_matches,
        'canonical_account_verified': False}
