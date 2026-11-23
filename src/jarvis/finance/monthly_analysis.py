"""Phase 6: deterministic monthly analytics, simple forecast, scenarios, charts."""
from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from jarvis.finance.ledger import cashflow

CACHE: dict[tuple[str, str, str, str], dict[str, Any]] = {}


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end


def previous_month(day: date) -> date:
    start = day.replace(day=1)
    return (start - timedelta(days=1)).replace(day=1)


def ledger_revision(cfo: Any) -> str:
    with cfo.db.connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id),0), COUNT(*) FROM transactions").fetchone()
        corrections = conn.execute("SELECT COUNT(*) FROM category_corrections").fetchone()[0]
    return hashlib.sha256(f'{row[0]}:{row[1]}:{corrections}'.encode()).hexdigest()[:16]


def _flow(cfo: Any, start: date, end: date, currency: str) -> dict[str, Any]:
    rows = cfo.economic_rows(cfo.db.transactions_between(start, end))
    flow = cashflow(rows, currency=currency)
    return {'flow': flow, 'rows': rows}


def monthly_report(cfo: Any, month: date, *, currency: str = 'RUB') -> dict[str, Any]:
    start, end = month_bounds(month)
    prior_start, prior_end = month_bounds(previous_month(month))
    revision = ledger_revision(cfo)
    key = (str(cfo.db.path.resolve()), revision, start.isoformat(), currency)
    if key in CACHE:
        return CACHE[key]
    current = _flow(cfo, start, end, currency)
    prior = _flow(cfo, prior_start, prior_end, currency)
    current_flow, prior_flow = current['flow'], prior['flow']
    cur_cat = {k: Decimal(v) for k, v in current_flow['by_category'].items()}
    pri_cat = {k: Decimal(v) for k, v in prior_flow['by_category'].items()}
    contributions = [{'category': key_name,
                      'current': str(cur_cat.get(key_name, Decimal(0))),
                      'prior': str(pri_cat.get(key_name, Decimal(0))),
                      'delta': str(cur_cat.get(key_name, Decimal(0)) - pri_cat.get(key_name, Decimal(0)))}
                     for key_name in sorted(set(cur_cat) | set(pri_cat))]
    contributions.sort(key=lambda item: Decimal(item['delta']), reverse=True)
    days = max(1, (end - start).days + 1)
    expenses = Decimal(current_flow['expenses'])
    prior_expenses = Decimal(prior_flow['expenses'])
    count = len([row for row in current['rows'] if Decimal(str(row['amount'])) < 0])
    prior_count = len([row for row in prior['rows'] if Decimal(str(row['amount'])) < 0])
    unresolved = sum(1 for row in current['rows']
                     if row['category'] in {'unknown', 'uncategorized', 'pending_review'})
    transfers = sum(1 for row in current['rows'] if row['category'] == 'internal_transfer')
    total_delta = expenses - prior_expenses
    contribution_sum = sum((Decimal(item['delta']) for item in contributions), Decimal(0))
    report = {
        'month': [start.isoformat(), end.isoformat()],
        'prior_month': [prior_start.isoformat(), prior_end.isoformat()],
        'currency': currency,
        'expenses': str(expenses), 'prior_expenses': str(prior_expenses),
        'expense_delta': str(total_delta),
        'income': str(current_flow['income']),
        'average_daily_expense': str((expenses / days).quantize(Decimal('0.01'))),
        'transaction_count': count, 'prior_transaction_count': prior_count,
        'average_ticket': str((expenses / count).quantize(Decimal('0.01'))) if count else None,
        'category_contributions': contributions,
        'top_growth': [item for item in contributions if Decimal(item['delta']) > 0][:5],
        'top_decline': [item for item in reversed(contributions) if Decimal(item['delta']) < 0][:5],
        'new_categories': [item['category'] for item in contributions
                           if item['prior'] == '0' and Decimal(item['current']) > 0],
        'gone_categories': [item['category'] for item in contributions
                            if item['current'] == '0' and Decimal(item['prior']) > 0],
        'contributions_sum': str(contribution_sum),
        'unexplained_difference': str(total_delta - contribution_sum),
        'unresolved_money_rows': unresolved,
        'confirmed_internal_transfers': transfers,
        'coverage_complete': False,
        'ledger_revision': revision,
        'provenance': ['ledger transactions', 'category_corrections', 'cashflow Decimal'],
        'financial_apply': False,
        'limitations': ['Known expenses are not the complete financial picture',
                        'Transfers and unresolved money roles are not expenses'],
    }
    CACHE[key] = report
    return report


def forecast(cfo: Any, *, months: int = 6, currency: str = 'RUB', now: date | None = None) -> dict[str, Any]:
    today = now or datetime.now(UTC).date()
    end_month = previous_month(today)
    series = []
    for offset in range(months - 1, -1, -1):
        month = end_month
        for _ in range(offset):
            month = previous_month(month)
        start, end = month_bounds(month)
        series.append((start.isoformat(), Decimal(_flow(cfo, start, end, currency)['flow']['expenses'])))
    nonempty = [value for _, value in series if value > 0]
    if len(nonempty) < 3:
        return {'status': 'INSUFFICIENT_DATA', 'reason': 'Need at least 3 months with known expenses',
                'series': [[label, str(value)] for label, value in series],
                'forecast': None, 'financial_apply': False}
    train, actual = series[:-1], series[-1][1]
    baseline = sum((value for _, value in train), Decimal(0)) / len(train)
    error = abs(baseline - actual) / actual if actual else Decimal(1)
    last = [value for _, value in series[-3:]]
    prediction = (sum(last, Decimal(0)) / len(last)).quantize(Decimal('0.01'))
    spread = (prediction * min(error, Decimal('0.5'))).quantize(Decimal('0.01'))
    return {'status': 'OK', 'method': 'rolling_3_month_mean', 'history_months': len(series),
            'backtest_absolute_percentage_error': str(error.quantize(Decimal('0.0001'))),
            'forecast': str(prediction), 'range': [str(prediction - spread), str(prediction + spread)],
            'currency': currency, 'assumptions': ['Known-expense history is comparable',
                'No new recurring obligations or income'],
            'limitations': ['Point forecast is not a guaranteed balance', 'Coverage is not complete'],
            'series': [[label, str(value)] for label, value in series],
            'financial_apply': False}


def monthly_series(cfo: Any, start: date, end: date, *, currency: str = 'RUB') -> dict[str, Any]:
    """Comparable full-month reports across a bounded range, one ledger revision."""
    if end < start:
        raise ValueError('End month before start')
    cursor = start.replace(day=1)
    reports = []
    while cursor <= end:
        reports.append(monthly_report(cfo, cursor, currency=currency))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    # Compact aggregates: the chat tool result is bounded, so full category maps
    # and per-transaction detail are not serialized.
    months = [{key: report[key] for key in (
        'month', 'currency', 'expenses', 'prior_expenses', 'expense_delta',
        'income', 'transaction_count', 'average_ticket', 'unresolved_money_rows',
        'confirmed_internal_transfers', 'coverage_complete', 'ledger_revision')}
        | {'top_growth': [{'category': item['category'], 'delta': item['delta'],
                          'current': item['current']} for item in report['top_growth'][:3]],
           'top_decline': [{'category': item['category'], 'delta': item['delta'],
                            'current': item['current']} for item in report['top_decline'][:3]],
           'new_categories': report['new_categories'][:3],
           'gone_categories': report['gone_categories'][:3]}
        for report in reports]
    deltas = [{'from': reports[i - 1]['month'][0], 'to': reports[i]['month'][0],
               'expenses': reports[i]['expenses'], 'prior': reports[i - 1]['expenses'],
               'delta': str(Decimal(reports[i]['expenses']) - Decimal(reports[i - 1]['expenses']))}
              for i in range(1, len(reports))]
    return {'status': 'OK', 'period': [reports[0]['month'][0], reports[-1]['month'][1]],
            'currency': currency, 'months': months, 'monthly_deltas': deltas,
            'monthly_expenses': [{'month': m['month'][0], 'expenses': m['expenses'],
                                  'prior_expenses': m['prior_expenses'],
                                  'expense_delta': m['expense_delta']} for m in months],
            'ledger_revision': reports[0]['ledger_revision'],
            'coverage_complete': False, 'financial_apply': False,
            'provenance': ['ledger transactions', 'category_corrections', 'cashflow Decimal'],
            'limitations': ['Known expenses are not the complete financial picture',
                            'Transfers and unresolved money roles are not expenses']}


def recommendations(cfo: Any, month: date, *, currency: str = 'RUB',
                    percent: Decimal = Decimal(10)) -> dict[str, Any]:
    """Up to three local category-reduction scenarios with explicit assumptions."""
    report = monthly_report(cfo, month, currency=currency)
    base = Decimal(report['expenses'])
    if base <= 0:
        return {'status': 'INSUFFICIENT_DATA', 'reason': 'No known expenses for this month',
                'scenarios': [], 'financial_apply': False}
    growth = [item for item in report['top_growth'] if Decimal(item['delta']) > 0]
    scenarios = []
    for item in growth[:3]:
        current = Decimal(item['current'])
        reduction = (current * percent / Decimal(100)).quantize(Decimal('0.01'))
        scenarios.append({'category': item['category'], 'baseline_expenses': str(base),
                          'category_current': str(current), 'assumed_reduction_percent': str(percent),
                          'effect': str(-reduction), 'adjusted_expenses': str(base - reduction),
                          'limitations': ['Assumes the category can be reduced without substitution',
                                          'Not a guaranteed future saving']})
    if not scenarios:
        return {'status': 'INSUFFICIENT_DATA', 'reason': 'No positive category growth to reduce',
                'scenarios': [], 'financial_apply': False}
    return {'status': 'OK', 'month': report['month'], 'currency': currency,
            'scenarios': scenarios, 'assumptions': ['Category reduction is hypothetical'],
            'financial_apply': False, 'ledger_revision': report['ledger_revision']}


def scenario(cfo: Any, month: date, changes: list[dict[str, Any]], *, currency: str = 'RUB') -> dict[str, Any]:
    report = monthly_report(cfo, month, currency=currency)
    base = Decimal(report['expenses'])
    adjusted = base
    applied = []
    for change in changes:
        kind = change.get('type')
        amount = Decimal(str(change.get('amount', '0')))
        if kind == 'category_percent':
            category = str(change.get('category', ''))
            value = Decimal(report['category_contributions'][0]['current']) if not category else Decimal(
                next((item['current'] for item in report['category_contributions']
                      if item['category'] == category), '0'))
            delta = (value * amount / Decimal(100)).quantize(Decimal('0.01'))
            adjusted -= delta
            applied.append({'type': kind, 'category': category, 'percent': str(amount), 'delta': str(-delta)})
        elif kind == 'income' or kind == 'savings':
            adjusted -= amount
            applied.append({'type': kind, 'delta': str(-amount)})
        else:
            raise ValueError('Unsupported scenario change')
    return {'month': report['month'], 'baseline_expenses': str(base),
            'adjusted_expenses': str(adjusted), 'delta': str(adjusted - base),
            'changes': applied, 'assumptions': ['Changes are hypothetical, not applied'],
            'financial_apply': False, 'coverage_complete': False,
            'ledger_revision': report['ledger_revision']}


CHART_TYPES = {'monthly_trend', 'category_breakdown', 'period_comparison',
               'category_waterfall', 'recurring_payments', 'goal_progress', 'forecast'}


def chart_spec(kind: str, *, title: str, period: list[str], series: list[dict[str, Any]],
               currency: str, provenance: list[str], ledger_revision: str,
               completeness: bool = False, uncertainty: dict[str, Any] | None = None) -> dict[str, Any]:
    if kind not in CHART_TYPES:
        raise ValueError('Unsupported chart type')
    if not series or len(series) > 60:
        raise ValueError('Bounded chart series required')
    safe_title = str(title)[:120]
    if any(token in safe_title.casefold() for token in ('<script', 'javascript:', 'http://', 'https://')):
        raise ValueError('Unsafe chart title')
    clean = []
    for item in series:
        label = str(item.get('label', ''))[:80]
        value = Decimal(str(item.get('value')))
        if not value.is_finite() or '<' in label or 'javascript:' in label.casefold():
            raise ValueError('Invalid chart data')
        clean.append({'label': label, 'value': str(value)})
    return {'chart_type': kind, 'title': safe_title, 'period': period, 'series': clean,
            'currency': currency, 'provenance': provenance[:10],
            'ledger_revision': ledger_revision, 'completeness': completeness,
            'uncertainty': uncertainty, 'financial_apply': False}


def chart_from_report(report: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == 'category_breakdown':
        series = [{'label': item['category'], 'value': item['current']}
                  for item in report['category_contributions'] if Decimal(item['current']) != 0]
        return chart_spec(kind, title='Расходы по категориям', period=report['month'],
                          series=series, currency=report['currency'],
                          provenance=report['provenance'], ledger_revision=report['ledger_revision'],
                          completeness=report['coverage_complete'])
    if kind == 'period_comparison':
        return chart_spec(kind, title='Сравнение месяцев', period=report['month'],
                          series=[{'label': 'Текущий', 'value': report['expenses']},
                                  {'label': 'Предыдущий', 'value': report['prior_expenses']}],
                          currency=report['currency'], provenance=report['provenance'],
                          ledger_revision=report['ledger_revision'], completeness=False)
    if kind == 'category_waterfall':
        return chart_spec(kind, title='Вклад категорий', period=report['month'],
                          series=[{'label': item['category'], 'value': item['delta']}
                                  for item in report['top_growth'] + report['top_decline']],
                          currency=report['currency'], provenance=report['provenance'],
                          ledger_revision=report['ledger_revision'], completeness=False)
    raise ValueError('No local series for this chart type')
