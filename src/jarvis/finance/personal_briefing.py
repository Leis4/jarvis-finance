"""Shared, read-only daily intelligence over the existing CFO evidence."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from jarvis.finance.event_taxonomy import category_title
from jarvis.finance.insights.deterministic import InsightPolicy
from jarvis.finance.personal_briefing_view import render_daily_briefing
from jarvis.finance.personal_cfo import PersonalCFO, money
from jarvis.finance.personal_profile import business_date
from jarvis.finance.personal_queries import analytics


def build_briefing(data: dict[str, Any], analysis: dict[str, Any], *,
                   now: datetime) -> dict[str, Any]:
    """Compose evidence-linked events without a second financial calculation."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')
    position = data['financial_position']
    today = business_date(now)
    policy = InsightPolicy()
    start, end = analysis['periods']['month']['period']
    period = {'start': start, 'end': end}
    observed = (data.get('bank') or {}).get('observed_at')
    events: list[dict[str, Any]] = []

    def add(key: str, epistemic: str, title: str, reason: str,
            evidence: list[dict[str, Any]], label: str, screen: str,
            priority: int, bounds: dict[str, str] | None = None) -> None:
        identity = json.dumps([key, evidence, bounds or period], sort_keys=True, ensure_ascii=False)
        events.append({'id': hashlib.sha256(identity.encode()).hexdigest()[:24], 'insight_type': key,
            'epistemic': epistemic, 'title': title, 'reason': reason,
            'evidence': evidence, 'period': bounds or period,
            'action': {'label': label, 'screen': screen}, 'priority': priority,
            'confidence': 'not_calibrated' if epistemic in {'INFERENCE', 'FORECAST'} else 'source_bound'})

    if not data.get('bank_fresh'):
        add('bank', 'STALE' if observed else 'UNKNOWN', 'Обновите банковские данные',
            'Сохранённое наблюдение не подтверждает текущий остаток. Свободная сумма неизвестна.',
            [{'source': 'bank_snapshot', 'observed_at': observed, 'fresh': False}],
            'Проверить источник и авторизацию банка', 'sources', 0)
    missing = position.get('missing') or []
    if missing:
        add('profile', 'UNKNOWN', 'Не хватает данных для безопасного решения',
            'Нужно уточнить: ' + '; '.join(missing) + '. Неизвестное не подменяется нулём.',
            [{'source': 'financial_position', 'missing': missing,
              'profile_updated_at': position.get('profile_updated_at')}],
            'Уточнить профиль, резерв и обязательства', 'settings', 1)
    if not analysis.get('coverage_complete'):
        add('coverage', 'UNKNOWN', 'История операций неполна',
            'Разность известных сумм не доказывает рост расходов или его причину.',
            [{'source': 'analytics', 'coverage_complete': False,
              'transaction_ids': analysis['periods']['month']['transaction_ids']}],
            'Проверить периоды и импорт выписки', 'sources', 4)
    direction_ids = analysis['periods']['month'].get('direction_conflict_ids', [])
    identity_ids = analysis['periods']['month'].get('identity_conflict_ids', [])
    if identity_ids:
        add('identity', 'UNKNOWN', 'Сверьте идентичность банковских операций',
            'Повторный банковский ID связан с разными датами, суммами или валютами. Обе стороны не считаются подтверждённым доходом или расходом; исходные данные сохранены.',
            [{'source': 'ledger', 'transaction_ids': identity_ids}],
            'Сопоставить операции с подтверждённым банковским источником', 'operations', 1)
    if direction_ids:
        add('direction', 'UNKNOWN', 'Уточните назначение поступлений',
            'Положительные суммы с расходной категорией не считаются доходом или возвратом без подтверждения роли. Движение денег сохранено.',
            [{'source': 'ledger', 'transaction_ids': direction_ids}],
            'Проверить поступления и явно выбрать доход, возврат или перевод', 'operations', 1)
    current = analysis['periods'].get('week', {})
    previous = analysis['periods'].get('prior_week', {})
    if (len(current.get('transaction_ids', [])) >= policy.minimum_observations
            and len(previous.get('transaction_ids', [])) >= policy.minimum_observations):
        before = money(previous['expenses'])
        after = money(current['expenses'])
        delta = after - before
        if before > 0 and delta >= policy.minimum_change and delta * 100 >= before * policy.relative_change_percent:
            complete = (current.get('coverage_complete') is True and previous.get('coverage_complete') is True
                and not current.get('unresolved_count') and not previous.get('unresolved_count'))
            add('expense_change', 'FACT' if complete else 'UNKNOWN',
                'Известные расходы заметно выросли' if complete else 'Проверьте изменение известных расходов',
                f"За неделю {after} ₽, за предыдущую {before} ₽, разность {delta} ₽. "
                + ('Периоды сопоставимы; причина изменения не доказана.' if complete else
                   'Полнота периодов не доказана; это не доказанный рост ваших общих расходов.'),
                [{'source': 'analytics', 'current': current, 'previous': previous,
                  'policy': policy.model_dump(mode='json')}],
                'Сравнить категории и исходные операции', 'analytics', 3,
                {'start': previous['period'][0], 'end': current['period'][1]})
    headroom = position.get('headroom')
    if (headroom is not None and not missing and not position.get('blocking_missing')
            and data.get('bank_fresh') and money(headroom) < 0):
        add('liquidity', 'FACT', 'Расчётного защищённого остатка недостаточно',
            f'Запас после известных обязательств и защиты: {headroom} ₽. '
            'Это расчёт по текущему профилю, не обещание будущего банковского остатка.',
            [{'source': 'financial_position', 'headroom': headroom,
              'observed_at': observed, 'profile_updated_at': position.get('profile_updated_at')}],
            'Проверить ближайшие обязательства и отложить необязательные траты', 'settings', 1)
    for item in analysis['calendar']['upcoming_obligations'][:3]:
        if not today <= date.fromisoformat(item['date']) <= today + timedelta(days=7):
            continue
        add('obligation', 'OWNER_DECISION', 'Проверьте ближайшее обязательство',
            f"В профиле: {item['name']} · {item['amount']} ₽ · {item['date']}. "
            'Запись в календаре не подтверждает оплату.',
            [{'source': 'owner_profile', **item}], 'Проверить сумму и дату', 'settings', 2,
            {'start': item['date'], 'end': item['date']})
    rows = {row['id']: row for row in data.get('pattern_rows', data.get('transactions', []))}
    for pattern in data.get('patterns', []):
        if pattern['kind'] == 'unusual_expense_candidate':
            ids = [pattern['transaction_id']]
            title = 'Необычная трата требует проверки'
            reason = f"{pattern['amount']} ₽ · {category_title(pattern['category'])}. Кандидат по локальному правилу, не докниколайельство ошибки."
        elif pattern['kind'] == 'expense_candidate':
            ids = pattern['transaction_ids']
            title = 'Возможный регулярный платёж'
            reason = (f"{pattern['merchant']} · обычно {pattern['typical_net_expense']} ₽. "
                'Повторение не доказывает подписку; ожидаемое списание не добавлено в обязательства.')
        elif pattern['kind'] == 'recurring_price_change_candidate':
            ids = pattern['transaction_ids']
            title = 'Изменилась сумма регулярного платежа-кандидата'
            reason = (f"{pattern['merchant']}: было {pattern['previous_amount']} ₽, теперь {pattern['amount']} ₽. "
                'Сравнение операций не доказывает изменение тарифа или ненужную подписку.')
        else:
            continue
        # Only expose candidates whose operation references can be shown in this snapshot.
        if not ids or any(ident not in rows for ident in ids):
            continue
        dates = sorted(str(rows[ident]['tx_date']) for ident in ids)
        add('pattern', 'INFERENCE', title, reason,
            [{'source': 'ledger', 'transaction_ids': ids, 'rule': pattern['kind']}],
            'Проверить операции; подтвердить назначение', 'operations', 3,
            {'start': dates[0], 'end': dates[-1]})
    events.sort(key=lambda event: event['priority'])
    free = position.get('headroom')
    if missing or position.get('blocking_missing') or not data.get('bank_fresh'):
        free = None
    result = {'generated_at': now.astimezone(UTC).isoformat(), 'observed_at': observed,
        'position': {'bank_total': position.get('bank_total'), 'spendable': free},
        'cashflow': analysis['periods']['month'],
        'comparison': {'current': current, 'previous': previous,
                       'delta': analysis.get('week_expense_delta')},
        'patterns': data.get('patterns', []),
        'events': events, 'paid_calls': 0, 'database_modified': False,
        'notice': 'Факты, гипотезы и решения владельца разделены. Полнота истории не доказана.'}
    return {**result, 'text': render_daily_briefing(result)}


def daily_briefing(cfo: PersonalCFO, *, weekly: bool = False) -> dict[str, Any]:
    """Read the existing financial core; never send, import or invoke a model."""
    now = datetime.now(UTC)
    analysis = analytics(cfo)
    result = build_briefing(cfo.dashboard(now=now), analysis, now=now)
    if weekly:
        result['cashflow'] = analysis['periods']['week']
        result['text'] = render_daily_briefing(result).replace('Что важно сегодня', 'Что важно за неделю', 1)
    return result
