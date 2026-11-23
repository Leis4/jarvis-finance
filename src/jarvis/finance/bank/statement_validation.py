"""Independent checks for Russian bank statements before any ledger write.

The validator never proves correctness by absence of errors. It compares the
parser result with an independent pass over the document text, checks the
per-operation balance invariant when the document exposes balances, and marks
what it could not verify.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import groupby, pairwise, permutations
from typing import Any

from jarvis.finance.bank.savings_validation import savings_document, validate_savings
from jarvis.finance.domain import Transaction

TX_LINE = re.compile(r'^(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<time>\d{2}:\d{2})\s+(?P<rest>.+)$')
DETAIL_LINE = re.compile(r'^(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<auth>[A-ZА-Я0-9]{4,16})\s+(?P<description>.+)$')
MONEY = re.compile(r'[+\-−]?\d[\d\s\xa0]*[,.]\d{2}')
MASKED = re.compile(r'\*{2,}\s?(\d{4})')
HEADER = ('остаток', 'за период', 'дата формирования', 'страница', 'движение средств',
          'выписка по', 'сформировано', 'www.sberbank')
INTEREST = ('процент', 'капитализац')
TRANSFER = ('перевод между', 'между счетами', 'собственный перевод', 'на свой счёт')


def _money(value: str) -> Decimal:
    cleaned = value.replace('\xa0', ' ').replace(' ', '').replace('−', '-').replace(',', '.')
    try:
        return Decimal(cleaned)
    except InvalidOperation as error:
        raise ValueError(f'Bad statement amount: {value!r}') from error


def _lines(text: str) -> list[str]:
    return [' '.join(line.replace('\xa0', ' ').split()) for line in text.splitlines()]


def _when(day: str, clock: str) -> datetime:
    return datetime.strptime(f'{day} {clock}', '%d.%m.%Y %H:%M').replace(tzinfo=UTC)


def _signed_balance_chain(rows: list[dict[str, Any]], opening: Decimal, *,
                          processing: bool = False) -> tuple[int, Decimal]:
    """Use declared posting dates, or equal-minute operation order when unavailable."""
    current = opening
    checked = 0
    def key(row: dict[str, Any]) -> datetime:
        return _when(row['processing_date'], '00:00') if processing else _when(row['date'], row['time'])
    ordered = sorted(rows, key=key)
    for _, group in groupby(ordered, key=key):
        batch = list(group)
        edges: dict[Decimal, list[Decimal]] = defaultdict(list)
        expected = Counter((r['balance'] - r['signed_amount'], r['balance']) for r in batch)
        for before, after in expected:
            edges[before].extend([after] * expected[before, after])
        stack = [current]
        trail = []
        while stack:
            if edges.get(stack[-1]):
                stack.append(edges[stack[-1]].pop())
            else:
                trail.append(stack.pop())
        trail.reverse()
        if Counter(pairwise(trail)) != expected:
            return checked, current
        current = trail[-1]
        checked += len(batch)
    return checked, current


def _document_controls(lines: list[str], rows: list[dict[str, Any]],
                       transactions: list[Transaction]) -> dict[str, Any]:
    """Verify Sber control totals without asserting canonical account identity."""
    anchors = []
    totals: dict[str, Decimal] = {}
    for line in lines[:100]:
        match = re.fullmatch(r'Остаток на (\d{2}\.\d{2}\.\d{4}) (.+)', line, re.IGNORECASE)
        if match:
            anchors.append((_when(match[1], '00:00').date(), _money(match[2])))
        for label in ('Пополнение', 'Списание'):
            if line.startswith(label + ' ') and MONEY.findall(line):
                totals[label] = _money(MONEY.findall(line)[-1])
    available = (len(anchors) == 2 and len(totals) == 2 and bool(rows)
        and all(r['balance'] is not None and r['signed_amount'] is not None for r in rows))
    result: dict[str, Any] = {'present': bool(anchors or totals), 'available': available,
        'canonical_account_verified': False, 'financial_history_complete': False}
    if not available:
        return result
    credits = sum((r['signed_amount'] for r in rows if r['signed_amount'] > 0), Decimal(0))
    debits = -sum((r['signed_amount'] for r in rows if r['signed_amount'] < 0), Decimal(0))
    processing = bool(re.search(r'дата\s+обработки', '\n'.join(lines[:100]), re.IGNORECASE)) and all(r.get('processing_date') for r in rows)
    signed_checked, signed_closing = _signed_balance_chain(rows, anchors[0][1], processing=processing)
    signed_complete = signed_checked == len(rows)
    header = '\n'.join(lines[:100])
    period = re.search(r'за\s+период\s+(\d{2}\.\d{2}\.\d{4})\s*[—–-]\s*(\d{2}\.\d{2}\.\d{4})', header, re.IGNORECASE)
    window_matches = bool(period and (_when(period[1], '00:00').date(), _when(period[2], '00:00').date()) == (anchors[0][0], anchors[1][0]))
    currency_verified = bool(re.search(r'российский\s+рубль|\bRUB\b|\bруб\b', header, re.IGNORECASE)) and all(t.currency == 'RUB' for t in transactions)
    outside = sum(not anchors[0][0] <= _when(r['date'], r['time']).date() <= anchors[1][0] for r in rows)
    outside_processing = (sum(not anchors[0][0] <= _when(r['processing_date'], '00:00').date() <= anchors[1][0] for r in rows)
        if processing else None)
    direction = len(rows) == len(transactions) and all(
        _when(r['date'], r['time']).date() == tx.tx_date and r['signed_amount'] == tx.amount
        for r, tx in zip(rows, transactions, strict=False))
    result.update(header_equation_verified=anchors[0][1] + totals['Пополнение'] - totals['Списание'] == anchors[1][1],
        credits_verified=credits == totals['Пополнение'], debits_verified=debits == totals['Списание'],
        opening_balance_verified=signed_checked > 0,
        closing_balance_verified=signed_complete and signed_closing == anchors[1][1],
        operation_direction_verified=direction and signed_complete, outside_operation_window=outside,
        signed_balance_rows_checked=signed_checked, signed_balance_chain_verified=signed_complete,
        signed_order_basis='processing_date' if processing else 'operation_timestamp',
        outside_processing_window=outside_processing,
        document_period={'from':anchors[0][0].isoformat(), 'to':anchors[1][0].isoformat()},
        currency_verified=currency_verified, declared_window_verified=window_matches)
    result['money_verified'] = all(result[key] for key in ('header_equation_verified',
        'credits_verified', 'debits_verified', 'opening_balance_verified',
        'closing_balance_verified', 'operation_direction_verified', 'currency_verified'))
    result['coverage_verified'] = result['money_verified'] and (outside_processing if processing else outside) == 0 and window_matches
    return result


def validate_statement(text: str, transactions: list[Transaction], *,
                       source_format: str) -> dict[str, Any]:
    """Return a verdict plus the evidence used to reach it."""
    lines = _lines(text)
    is_pdf = source_format == 'pdf'
    savings = validate_savings(text, transactions) if is_pdf and savings_document(text) else None
    rows: list[dict[str, Any]] = []
    unparsed = 0
    ignored = 0
    for position, line in enumerate(lines):
        if not line:
            continue
        lowered = line.casefold()
        if any(word in lowered for word in HEADER):
            ignored += 1
            continue
        match = TX_LINE.match(line) if is_pdf else None
        if not match:
            continue
        values = MONEY.findall(match.group('rest'))
        if not values:
            unparsed += 1
            continue
        auth = None
        processing_date = None
        for follow in lines[position + 1:position + 4]:
            if TX_LINE.match(follow):
                break
            detail = DETAIL_LINE.match(follow)
            if detail:
                auth = detail.group('auth')
                processing_date = detail.group('date')
                break
        rows.append({'date': match.group('date'), 'time': match.group('time'),
                     'amount': _money(values[-2] if len(values) >= 2 else values[-1]),
                      'signed_amount': (abs(_money(values[-2])) if values[-2].strip().startswith('+') else -abs(_money(values[-2]))) if len(values) >= 2 else None,
                     'balance': _money(values[-1]) if len(values) >= 2 else None,
                     'auth': auth, 'rest': match.group('rest')})
        rows[-1]['processing_date'] = processing_date

    independent = len(rows) if is_pdf else None
    if savings:
        independent = savings['financial_rows']
        unparsed = savings['unparsed']
    duplicate_lines: Counter[tuple[str, str, str, str]] = Counter()
    for row in rows:
        if row['auth']:
            duplicate_lines[(row['date'], row['time'], str(row['amount']), row['auth'])] += 1
        else:
            rest = MONEY.sub(' ', row['rest']).strip()
            duplicate_lines[(row['date'], row['time'], str(row['amount']), rest)] += 1
    duplicate_operation_lines = sum(count - 1 for count in duplicate_lines.values() if count > 1)
    controls = _document_controls(lines, rows, transactions) if is_pdf and not savings else None
    if savings:
        duplicate_operation_lines = savings['duplicates']

    weak: Counter[tuple[str, str, str, str]] = Counter()
    for tx in transactions:
        weak[(tx.tx_date.isoformat(), str(tx.amount), tx.description,
              (tx.merchant or '').casefold())] += 1

    balance_checked = 0
    balance_mismatch = 0
    balance_available = is_pdf and bool(rows) and all(row['balance'] is not None for row in rows)
    balance_example = None
    amount_equals_balance = 0
    if balance_available:
        document_order = (_when(rows[0]['date'], rows[0]['time'])
                          > _when(rows[-1]['date'], rows[-1]['time']))
        ordered = list(reversed(rows)) if document_order else list(rows)
        grouped: list[list[dict[str, Any]]] = []
        for row in ordered:
            if grouped and (grouped[-1][0]['date'] == row['date']
                            and grouped[-1][0]['time'] == row['time']):
                grouped[-1].append(row)
            else:
                grouped.append([row])
        previous = grouped[0][0]['balance'] - grouped[0][0]['amount']
        first = True
        for group in grouped:
            if first:
                first = False
                ordered_group = group
                if len(group) > 1:
                    for option in permutations(group):
                        if all(option[pos]['balance'] + abs(option[pos + 1]['amount']) == option[pos + 1]['balance']
                               or option[pos]['balance'] - abs(option[pos + 1]['amount']) == option[pos + 1]['balance']
                               for pos in range(len(option) - 1)):
                            ordered_group = list(option)
                            break
                previous = ordered_group[0]['balance']
                for row in ordered_group[1:]:
                    if not (previous + abs(row['amount']) == row['balance']
                            or previous - abs(row['amount']) == row['balance']):
                        balance_mismatch += 1
                        if balance_example is None:
                            balance_example = {'date': row['date'], 'time': row['time'],
                                               'previous': str(previous), 'amount': str(row['amount']),
                                               'observed': str(row['balance'])}
                    previous = row['balance']
                    balance_checked += 1
                continue
            chosen = None
            if len(group) > 1:
                for option in permutations(group):
                    running = previous
                    for candidate in option:
                        if not (running + abs(candidate['amount']) == candidate['balance']
                                or running - abs(candidate['amount']) == candidate['balance']):
                            break
                        running = candidate['balance']
                    else:
                        chosen = list(option)
                        break
            for row in (chosen if chosen is not None else group):
                plus = previous + abs(row['amount']) == row['balance']
                minus = previous - abs(row['amount']) == row['balance']
                if not plus and not minus:
                    balance_mismatch += 1
                    if balance_example is None:
                        balance_example = {'date': row['date'], 'time': row['time'],
                                           'previous': str(previous), 'amount': str(row['amount']),
                                           'observed': str(row['balance'])}
                previous = row['balance']
                balance_checked += 1
        amount_equals_balance = sum(1 for row in ordered
                                    if row['balance'] != 0 and abs(row['amount']) == row['balance'])

    if savings:
        balance_available = bool(savings['checked'])
        balance_checked = savings['checked']
        balance_mismatch = savings['mismatch']
    elif controls and controls['available'] and controls['signed_order_basis'] == 'processing_date':
        balance_checked = controls['signed_balance_rows_checked']
        balance_mismatch = int(not controls['signed_balance_chain_verified'])
        balance_example = None
    zero_amount = sum(1 for tx in transactions if tx.amount == 0)
    currencies = sorted({tx.currency for tx in transactions})
    totals = {}
    for currency in currencies:
        incoming = sum((tx.amount for tx in transactions if tx.currency == currency and tx.amount > 0), Decimal(0))
        outgoing = sum((tx.amount for tx in transactions if tx.currency == currency and tx.amount < 0), Decimal(0))
        totals[currency] = {'incoming_total': str(incoming), 'outgoing_total': str(outgoing),
                            'net': str(incoming + outgoing)}
    single_currency = currencies[0] if len(currencies) == 1 and re.fullmatch(r'[A-Z]{3}', currencies[0]) else None
    single_total = totals[single_currency] if single_currency else {}
    descriptions = [tx.description.casefold() for tx in transactions]
    interest = sum(1 for tx, text_l in zip(transactions, descriptions, strict=True)
                   if any(word in text_l for word in INTEREST) or tx.category == 'interest')
    transfers = sum(1 for tx, text_l in zip(transactions, descriptions, strict=True)
                    if tx.category in {'transfer', 'internal_transfer'}
                    or any(word in text_l for word in TRANSFER))
    refunds = sum(1 for tx, text_l in zip(transactions, descriptions, strict=True)
                  if tx.category == 'refund' or 'возврат' in text_l)
    days = sorted(tx.tx_date.isoformat() for tx in transactions)
    masked: Counter[str] = Counter(match.group(1) for line in lines for match in MASKED.finditer(line))

    reasons: list[str] = []
    if savings:
        reasons.extend(savings['unsafe_reasons'])
    if controls and controls['available'] and not controls['money_verified']:
        reasons.append('document_control_mismatch')
    if zero_amount:
        reasons.append('zero_amount_rows')
    if is_pdf and independent != len(transactions):
        reasons.append('row_count_mismatch')
    if unparsed:
        reasons.append('unparsed_operation_lines')
    if duplicate_operation_lines:
        reasons.append('duplicate_operation_lines')
    if balance_mismatch:
        reasons.append('balance_mismatch')
    if amount_equals_balance and amount_equals_balance >= max(2, len(transactions) // 2):
        reasons.append('amount_matches_balance')
    if not transactions:
        reasons.append('no_transactions')

    if reasons:
        verdict = 'unsafe'
    elif (not is_pdf or not balance_available or savings and not savings['complete']
          or controls and controls['present'] and not controls.get('coverage_verified')):
        verdict = 'review'
    else:
        verdict = 'safe'
    return {
        'source_format': source_format,
        'document_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()[:16],
        'parsed_rows': len(transactions),
        'savings_check': savings,
        'document_controls': controls,
        'independent_operation_lines': independent,
        'row_parity': (independent == len(transactions)) if is_pdf else None,
        'unparsed_operation_lines': unparsed,
        'ignored_header_footer_lines': ignored,
        'duplicate_groups': sum(1 for count in weak.values() if count > 1),
        'duplicate_rows': sum(count - 1 for count in weak.values() if count > 1),
        'duplicate_operation_lines': duplicate_operation_lines,
        'zero_amount_rows': zero_amount,
        'balance_check': {'available': balance_available, 'checked': balance_checked,
                          'mismatch': balance_mismatch, 'example': balance_example,
                          'amount_equals_balance_rows': amount_equals_balance,
                          'scope': ('signed_savings_rows_and_document_anchors' if savings else
                              'signed_processing_day_transitions_and_document_anchors'
                              if controls and controls.get('signed_order_basis') == 'processing_date' else
                              'adjacent_absolute_amount_transitions'),
                          'opening_balance_verified': bool((savings or controls or {}).get('opening_balance_verified')),
                          'closing_balance_verified': bool((savings or controls or {}).get('closing_balance_verified')),
                          'operation_direction_verified': bool((savings or controls or {}).get('operation_direction_verified'))},
        'period': ({'from': days[0], 'to': days[-1], 'source': 'operations'} if days else None),
        'account_suffix': masked.most_common(1)[0][0] if masked else None,
        'kinds': {'incoming': sum(1 for tx in transactions if tx.amount > 0),
                  'outgoing': sum(1 for tx in transactions if tx.amount < 0),
                  'refunds': refunds, 'interest': interest, 'transfers': transfers,
                  'currency': single_currency, 'by_currency': totals,
                  'incoming_total': single_total.get('incoming_total'),
                  'outgoing_total': single_total.get('outgoing_total'), 'net': single_total.get('net')},
        'unsafe_reasons': reasons,
        'verdict': verdict,
        'can_import': verdict == 'safe',
        'notice': ('Подписанные суммы и остатки сверены с границами и контрольными итогами документа. Привязка банковского счёта к сохранённой истории не подтверждена.'
            if verdict == 'safe' and (savings or controls and controls.get('coverage_verified')) else {
            'safe': 'Строки совпали с независимым подсчётом; соседние остатки согласуются с модулями сумм. Начальный и конечный остатки документа и направления операций отдельно не подтверждены.',
            'review': 'Часть проверок недоступна или требует внимания. Импорт только с явным подтверждением.',
            'unsafe': 'Проверка не пройдена. Массовый импорт заблокирован.',
        }[verdict]),
    }
