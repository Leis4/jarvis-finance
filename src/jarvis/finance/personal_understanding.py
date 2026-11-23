"""Reviewed local question routing; canonical finance services own every amount."""
from __future__ import annotations

import re
from typing import Any

from jarvis.finance.event_taxonomy import category_title
from jarvis.finance.ledger import direction_conflict
from jarvis.finance.personal_briefing import daily_briefing
from jarvis.finance.personal_cfo import PersonalCFO, money

_SPACES = re.compile(r"\s+")

_ID = re.compile(r"#\s*(\d{1,12})")

_OP_ID = re.compile(r"\bопераци[а-яё]*\s*(?:№|#|номер)?\s*(\d{1,12})\b", re.IGNORECASE)

_WRITE_COMMANDS = re.compile(
    r"\b(запиши|записать|добавь|добавить|сохрани|сохранить|создай|создать|"
    r"внеси|внести|отметь|отметить|удали|удалить|исправь|исправить|"
    r"поменяй|поменять|переведи|перевести|оплати|оплатить|верни|вернуть|"
    r"купил|купила|купили)\b",
    re.IGNORECASE,
)

def _normalise(text: object) -> str:
    value = str(text or "").casefold().replace("ё", "е")
    return _SPACES.sub(" ", value).strip()

def _write_request(normalised: str) -> bool:
    """Recording/editing commands belong to the write-aware surfaces, not search."""
    return bool(_WRITE_COMMANDS.search(normalised))

def _extract_id(normalised: str) -> int | None:
    match = _ID.search(normalised)
    if match:
        return int(match.group(1))
    match = _OP_ID.search(normalised)
    if match:
        return int(match.group(1))
    return None

def _is_explain_intent(normalised: str) -> bool:
    has_operation = "операци" in normalised
    has_category = "категор" in normalised
    asks_why = any(word in normalised for word in ("почему", "объясн", "причин", "откуда"))
    if asks_why and has_operation:
        return True
    return has_operation and has_category and _extract_id(normalised) is not None

def _is_attention_intent(normalised: str) -> bool:
    return any(
        phrase in normalised
        for phrase in (
            "что требует внимания",
            "что важно сейчас",
            "что важно сегодня",
            "на что обратить внимание",
            "сводка на сегодня",
            "сводка за сегодня",
            "что происходит сейчас",
        )
    )

def _is_spendable_intent(normalised: str) -> bool:
    if any(
        phrase in normalised
        for phrase in (
            "можно потратить",
            "можно ли потратить",
            "сколько свободно",
            "сколько доступно",
            "свободные средства",
            "свободных средств",
            "доступные средства",
            "доступных средств",
        )
    ):
        return True
    return (
        "сколько" in normalised
        and "денег" in normalised
        and any(word in normalised for word in ("потрат", "остал", "есть"))
    )

def _is_subscription_intent(normalised: str) -> bool:
    if "подписк" in normalised:
        return True
    return "лишн" in normalised and any(
        word in normalised for word in ("платеж", "списан", "подписк")
    )

def _is_recurring_intent(normalised: str) -> bool:
    return any(word in normalised for word in ("повтор", "регулярн")) and any(
        word in normalised for word in ("платеж", "списан", "расход")
    )

def _is_top_intent(normalised: str) -> bool:
    return any(
        phrase in normalised
        for phrase in (
            "больше всего",
            "куда уход",
            "куда ушли",
            "куда уходят",
            "крупные расход",
            "самые крупн",
            "самые больш",
            "топ расход",
            "на что я трат",
            "на что трат",
            "больше трат",
        )
    )

def _is_weekly_intent(normalised: str) -> bool:
    if "недел" not in normalised:
        return False
    return any(
        word in normalised
        for word in ("измен", "динамик", "сравн", "расход", "трат", "потрат")
    )

def _is_search_intent(normalised: str) -> bool:
    if any(word in normalised for word in ("найди", "найти", "поищи", "поиск")):
        return True
    if "где я" in normalised:
        return True
    return "покажи" in normalised and any(
        word in normalised
        for word in ("операци", "платеж", "покупк", "списан", "перевод", "трат")
    )

def _search_query(normalised: str) -> str:
    query = re.sub(
        r"\b(найди|найти|поищи|поиск|покажи|пожалуйста|операции|операцию|"
        r"операция|платежи|платеж|покупки|покупку|покупка|списания|списание|"
        r"переводы|перевод|траты|мне|все|всех)\b",
        " ",
        normalised,
    )
    query = re.sub(r"\bгде\s+я\b", " ", query)
    query = _SPACES.sub(" ", query).strip(" ?.,!;:-—")
    return query[:100]


def _result(status: str, text: str, data: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    if len(text) > 3900:
        status, text = 'clarification_required', 'Результат слишком длинный. Уточните операцию или поисковый запрос; суммы не сокращаю.'
    return {'status': status, 'text': text, 'data': data, 'sources': sources,
        'paid_cost_rub': '0', 'financial_apply': False,
        'tool_trace': [{'name': source.get('function', source['source']), **source} for source in sources]}


def _category_reason(cfo: PersonalCFO, text: str) -> dict[str, Any]:
    ident = _extract_id(text)
    if ident is None:
        return _result('clarification_required', 'Укажите ID операции: «Почему операция #123 получила эту категорию?»', {}, [])
    with cfo.db.connect() as conn:
        raw = conn.execute('SELECT * FROM transactions WHERE id=?', (ident,)).fetchone()
        correction = conn.execute('SELECT version,old_category,new_category,source,created_at '
            'FROM category_corrections WHERE transaction_id=? ORDER BY version DESC LIMIT 1', (ident,)).fetchone()
    if raw is None:
        return _result('unavailable', 'Операция не найдена. Причину и категорию не выдумываю.', {'transaction_id': ident}, [])
    tx = cfo.economic_rows([dict(raw)])[0]
    latest = dict(correction) if correction else None
    confirmed = bool(latest and latest['source'] == 'manual' and latest['new_category'] == tx['category'])
    header = f"Операция #{ident}: {tx['amount']} {tx['currency']}, категория «{category_title(tx['category'])}». "
    if confirmed:
        assert latest is not None
        reason = f"Подтверждена владельцем {latest['created_at']}, версия {latest['version']}. Решение владельца не заменяется догадкой модели."
    elif tx.get('category_origin') == 'bank_resource_proof':
        reason = 'Собственный перевод подтверждён связью банковских ресурсов и исключён из доходов и расходов.'
    else:
        reason = 'Категория сохранена в журнале; подтверждённая причина её выбора неизвестна. Уверенность классификатора не измерена.'
    role_unknown = bool(tx.get('identity_conflict')) or direction_conflict(money(tx['amount']), tx['category'])
    if tx.get('identity_conflict'):
        reason += ' Банковский ID связан с разными денежными данными. Денежная роль UNKNOWN до сверки источников; сумма и метка сохранены.'
    elif role_unknown:
        reason += ' Поступление с расходной категорией не считается доходом: уточните, это возврат или другое движение денег.'
    sources = [{'source': 'ledger', 'transaction_id': ident, 'period': [tx['tx_date'], tx['tx_date']]}]
    if latest:
        sources.append({'source': 'category_corrections', 'transaction_id': ident, **latest})
    return _result('evidence_only' if confirmed or tx.get('category_origin') else 'insufficient_data',
        header + reason, {'transaction_id': ident, 'category': tx['category'], 'amount': tx['amount'],
        'currency': tx['currency'], 'owner_confirmed': confirmed, 'latest_correction': latest,
        'role_unknown': role_unknown}, sources)


def answer(cfo: PersonalCFO, text: str) -> dict[str, Any] | None:
    """Route supported phrases over one shared intelligence service, without writes."""
    normalised = _normalise(text)
    if not normalised or _write_request(normalised):
        return None
    if _is_explain_intent(normalised):
        return _category_reason(cfo, normalised)
    if _is_attention_intent(normalised) or any(term in normalised for term in (
            'что происходит с моими деньгами', 'что мне стоит сделать', 'что делать дальше')):
        brief = daily_briefing(cfo)
        return _result('review', str(brief['text']), brief, [{'source': 'intelligence', 'function': 'daily_briefing'}])
    if _is_spendable_intent(normalised):
        brief = daily_briefing(cfo)
        value = brief['position']['spendable']
        text = ('Доступная сумма НЕИЗВЕСТНА: уточните свежий баланс и профиль. Не подставляю ноль или сценарный прогноз.'
            if value is None else f'Свободно к трате по известным данным: {value} ₽. Это расчёт с ограничениями полноты истории, не гарантия.')
        return _result('insufficient_data' if value is None else 'evidence_only', text,
            {'position': brief['position'], 'events': brief['events'][:3]},
            [{'source': 'financial_position', 'observed_at': brief['observed_at']}])
    if _is_subscription_intent(normalised) or _is_recurring_intent(normalised):
        brief = daily_briefing(cfo)
        patterns = [p for p in brief['patterns'] if p['kind'] in {'expense_candidate', 'recurring_price_change_candidate'}][:8]
        if not patterns:
            return _result('insufficient_data', 'Повторяющиеся платежи-кандидаты не найдены. Это не доказывает отсутствие подписок.', {}, [])
        lines = []
        for p in patterns:
            cost = (f"{p['previous_amount']} → {p['amount']} ₽" if p['kind'] == 'recurring_price_change_candidate'
                else f"обычно {p['typical_net_expense']} ₽")
            if p.get('monthly_equivalent') is not None:
                cost += f" (эквивалент ≈{p['monthly_equivalent']} ₽/мес, не фактический расход месяца)"
            lines.append(f"{str(p['merchant'])[:100]}: {cost}")
        return _result('review', 'Кандидаты регулярных списаний: ' + '; '.join(lines)
            + '. Это не доказанные подписки. Ненужность услуги нельзя установить по одним списаниям; проверьте использование и условия отмены.',
            {'patterns': patterns}, [{'source': 'ledger', 'transaction_ids': p['transaction_ids']} for p in patterns])
    if _is_top_intent(normalised):
        brief = daily_briefing(cfo, weekly=any(term in normalised for term in ('недел', '7 дней', 'семь дней')))
        flow = brief['cashflow']
        items = sorted(flow['by_category'].items(), key=lambda item: money(item[1]), reverse=True)[:5]
        bounds = flow['period']
        text = (f'За {bounds[0]} — {bounds[1]} больше всего известных расходов: '
            + '; '.join(f'{category_title(key)}: {value} ₽' for key, value in items)
            + '. Собственные переводы исключены; полнота истории и часть назначений неизвестны.' if items else
            'Нет известных расходов по категориям за этот месяц. Отсутствие данных не означает ноль.')
        return _result('evidence_only' if items else 'insufficient_data', text, {'period': bounds, 'by_category': dict(items)},
            [{'source': 'ledger', 'transaction_ids': flow['transaction_ids'], 'period': bounds}])
    if _is_weekly_intent(normalised) or ('что изменилось' in normalised and 'месяц' not in normalised):
        brief = daily_briefing(cfo, weekly=True)
        comparison = brief['comparison'];a = comparison['current'];b = comparison['previous']
        if not a['transaction_ids'] or not b['transaction_ids']:
            return _result('insufficient_data', 'В одном из сравниваемых периодов недостаточно записей. Это не означает нулевые расходы; рост и причина неизвестны.', comparison, [])
        return _result('evidence_only', f"За {a['period'][0]} — {a['period'][1]} известно расходов {a['expenses']} ₽; "
            f"за {b['period'][0]} — {b['period'][1]}: {b['expenses']} ₽; разность {comparison['delta']} ₽. "
            'Это сравнение известных записей, не доказанный рост всех расходов. Полнота и причина изменения неизвестны.', comparison,
            [{'source': 'analytics', 'current_ids': a['transaction_ids'], 'previous_ids': b['transaction_ids']}])
    if _is_search_intent(normalised):
        from jarvis.finance.search import hybrid_search
        query = _search_query(normalised)
        if not query:
            return _result('clarification_required', 'Укажите название или описание операции для поиска.', {}, [])
        rows = hybrid_search(cfo.db.path, query, limit=8)
        lines = [f"#{r['id']}: {r['amount']} {r['currency']}, {str(r['merchant'])[:100]}, {r['tx_date']}" for r in rows]
        return _result('evidence_only' if rows else 'insufficient_data',
            ('Найденные операции: ' + '; '.join(lines) if rows else 'Совпадений нет. История неполна; это не доказывает отсутствие операций.'),
            {'items': rows, 'query': query}, [{'source': 'ledger', 'transaction_ids': [r['id'] for r in rows]}])
    return None
