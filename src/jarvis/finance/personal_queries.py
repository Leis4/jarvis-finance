"""Validated read-only financial queries; Decimal calculates, language names tools."""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jarvis.finance.event_taxonomy import (
    NON_ECONOMIC_CATEGORIES,
    UNRESOLVED_CATEGORIES,
    canonical_category,
    category_title,
)
from jarvis.finance.ledger import cashflow
from jarvis.finance.personal_cfo import PersonalCFO, money
from jarvis.finance.personal_profile import business_date, dates


def business_today() -> date:
    return business_date(datetime.now(UTC))


NOTICE = 'Известная часть истории; полнота не доказана. Собственные переводы исключены. Неизвестные назначения не распределены догадками.'


class ReadToolRequest(BaseModel):
    """Closed tool names and validated dates; no SQL, paths or write instructions."""
    model_config = ConfigDict(extra='forbid')
    name: Literal['position', 'period', 'goals', 'review', 'patterns', 'purchase']
    start: date | None = None
    end: date | None = None
    category: str | None = Field(default=None, pattern=r'^[a-z_][a-z0-9_:]{0,79}$')
    price: str | None = Field(default=None, max_length=30)

    @model_validator(mode='after')
    def validate_scope(self) -> ReadToolRequest:
        today = business_today()
        if self.name == 'period':
            if self.start is None or self.end is None or not self.start <= self.end <= today or (self.end-self.start).days > 365:
                raise ValueError('A historical period of at most 366 days is required')
        elif self.start is not None or self.end is not None or self.category is not None:
            raise ValueError('Dates/category belong to the period tool')
        if self.name == 'purchase':
            if self.price is None or not 0 < money(self.price) <= 1_000_000_000:
                raise ValueError('A positive bounded purchase price is required')
        elif self.price is not None:
            raise ValueError('Price belongs to purchase tool')
        return self


def period(cfo: PersonalCFO, start: date, end: date, category: str | None = None) -> dict[str, Any]:
    request = ReadToolRequest(name='period', start=start, end=end, category=category)
    rows = cfo.economic_rows(cfo.db.transactions_between(start, end))
    if request.category:
        rows = [r for r in rows if canonical_category(r['category']) == canonical_category(request.category)]
    flow = cashflow(rows)
    return {'period': [start.isoformat(), end.isoformat()], 'currency': 'RUB',
        **{key: str(flow[key]) for key in ('income', 'expenses', 'movement', 'excluded_movement', 'unresolved_movement')},
        'by_category': {k: str(v) for k,v in flow['by_category'].items()},
        'transaction_ids': flow['transaction_ids'], 'coverage_complete': False,
        'direction_conflict_ids': flow['direction_conflict_ids'],
        'direction_conflict_count': flow['direction_conflict_count'],
        'identity_conflict_ids': flow['identity_conflict_ids'],
        'identity_conflict_count': flow['identity_conflict_count'],
        'unresolved_count': len({r['id'] for r in rows if canonical_category(r['category']) in UNRESOLVED_CATEGORIES}
            | set(flow['direction_conflict_ids']) | set(flow['identity_conflict_ids'])),
        'notice': NOTICE}


def read_tool(cfo: PersonalCFO, request: ReadToolRequest) -> dict[str, Any]:
    """Execute only the validated allowlist against the existing financial core."""
    if request.name == 'period':
        assert request.start is not None and request.end is not None
        return period(cfo, request.start, request.end, request.category)
    if request.name == 'position':
        position: dict[str, Any] = cfo.dashboard()['financial_position']
        return position
    if request.name == 'purchase':
        assert request.price is not None
        return cfo.purchase(request.price)
    if request.name == 'goals':
        return {'goals': cfo.goals(business_today())}
    if request.name == 'patterns':
        return {'patterns': cfo.dashboard()['patterns'], 'notice': 'Повторение — кандидат, не доказанная подписка или лишняя покупка.'}
    return {'review': cfo.data_status()['review'], 'financial_apply': False}


def analytics(cfo: PersonalCFO) -> dict[str, Any]:
    today = business_today(); month = today.replace(day=1)
    previous_end = month-timedelta(days=1)
    windows = {'today': (today,today), 'week': (today-timedelta(days=6),today),
        'prior_week': (today-timedelta(days=13),today-timedelta(days=7)),
        'month': (month,today), 'previous_month': (previous_end.replace(day=1),previous_end)}
    summaries = {name:period(cfo,*bounds) for name,bounds in windows.items()}
    food = sum((money(v) for k,v in summaries['month']['by_category'].items() if k in {'food','food_basic'}), Decimal(0))
    limits = cfo.db.get_setting('cfo.category_limits', {})
    food_limit = limits.get('food_basic',limits.get('food'))
    remaining = money(food_limit)-food if food_limit is not None else None
    days = calendar.monthrange(today.year,today.month)[1]-today.day+1
    profile=cfo.profile()
    upcoming=[]
    for item in profile.obligations:
        for day in dates(item,today,today+timedelta(days=30)):
            upcoming.append({'name':item.name,'date':day.isoformat(),'amount':item.amount,'certainty':item.certainty})
    confirmed_incomes: list[dict[str,Any]]=[]
    expected: list[dict[str,Any]]=[]
    for item in profile.incomes:
        expanded=dates(item,today,today+timedelta(days=90))
        if expanded:
            value={'name':item.name,'date':expanded[0].isoformat(),'amount':item.amount,'certainty':item.certainty}
            (confirmed_incomes if item.certainty=='confirmed' else expected).append(value)
    return {'periods':summaries, 'week_expense_delta':str(money(summaries['week']['expenses'])-money(summaries['prior_week']['expenses'])),
        'comparison': 'Разность известных сумм, не доказанный рост: покрытие и длины месяцев различаются.',
        'food': {'month_spent':str(food), 'daily_known_average':str((food/today.day).quantize(Decimal('.01'))),
            'limit':food_limit, 'remaining':str(remaining) if remaining is not None else None,
            'remaining_days':days, 'daily_budget':str((max(Decimal(0),remaining)/days).quantize(Decimal('.01'),rounding=ROUND_DOWN)) if remaining is not None else None,
            'notice':'Верхняя граница заданного бюджета по известным данным; не медицинский норматив и не совет сокращать питание.'},
        'calendar':{'upcoming_obligations':sorted(upcoming,key=lambda e:e['date'])[:12],
            'obligations_complete':profile.obligations_complete,
            'next_confirmed_income':min(confirmed_incomes,key=lambda e:e['date']) if confirmed_incomes else None,
            'expected_incomes':sorted(expected,key=lambda e:e['date'])[:5],
            'undated_incomes':[item.name for item in profile.incomes if item.enabled and item.next_date is None],
            'timezone':'Europe/Moscow'}, 'coverage_complete':False}


class HistoryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: date | None = None
    end: date | None = None
    q: str = Field(default='',max_length=100)
    category: str = Field(default='',pattern=r'^$|^[a-z_][a-z0-9_:]{0,79}$')
    kind: Literal['all','income','expense','transfer','unknown'] = 'all'
    confirmation: Literal['all','pending','confirmed'] = 'all'
    source: str = Field(default='',pattern=r'^[a-zA-Z0-9_-]{0,40}$')
    offset: int = Field(default=0,ge=0,le=5000)
    limit: int = Field(default=20,ge=1,le=100)


def history(cfo: PersonalCFO, request: HistoryRequest) -> dict[str, Any]:
    """Filter the actual bounded period, not only the dashboard's latest200 rows."""
    today=business_today();start=request.start or today-timedelta(days=89);end=request.end or today
    ReadToolRequest(name='period',start=start,end=end)
    rows=cfo.economic_rows(cfo.db.transactions_between(start,end))
    if len(rows)>5000:
        return {'status':'refine_period','items':[],'total':None,'next_offset':None,'notice':'Больше5000операций: сузьте период.'}
    with cfo.db.connect() as conn:
        corrections={int(r[0]):dict(r) for r in conn.execute('''SELECT transaction_id,version,source,new_category FROM category_corrections c
            WHERE version=(SELECT MAX(version) FROM category_corrections WHERE transaction_id=c.transaction_id)''')}
        origins: dict[int,set[str]]={}
        for row in conn.execute('SELECT DISTINCT transaction_id,source FROM source_events WHERE transaction_id IS NOT NULL'):
            origins.setdefault(int(row[0]),set()).add(row[1])
        proposals={int(r[0].split(':')[-1]):json.loads(r[1]) for r in conn.execute("SELECT key,value FROM settings WHERE key LIKE 'cfo.ai_proposal:%'")}
    result=[];categories=set();sources=set();q=request.q.casefold().strip()
    for row in rows:
        category=canonical_category(row['category']);categories.add(category)
        row_sources=origins.get(row['id'],set());sources.update(row_sources)
        correction=corrections.get(row['id'],{})
        confirmed=correction.get('source')=='manual' and correction.get('new_category')==row['category']
        kind='transfer' if category in NON_ECONOMIC_CATEGORIES else 'unknown' if category in UNRESOLVED_CATEGORIES else 'income' if money(row['amount'])>0 else 'expense'
        if (q and q not in (row['merchant']+' '+row['description']).casefold()
                or request.category and request.category!=category
                or request.source and request.source not in row_sources
                or request.kind!='all' and request.kind!=kind
                or request.confirmation=='confirmed' and not confirmed
                or request.confirmation=='pending' and confirmed):
            continue
        proposal=proposals.get(row['id'],{}).get('result',{})
        proposed_reason = ('Предложение '+str(proposal.get('category', 'unknown'))+' (не применено): '+str(proposal.get('reason_text', 'Нужно уточнение владельца.'))) if proposal else None
        reason='Подтверждено владельцем' if confirmed else 'Свой перевод: доказана связь банковских ресурсов' if row.get('category_origin')=='bank_resource_proof' else proposed_reason or 'Категория из журнала; отдельное подтверждение владельца отсутствует'
        result.append({**row,'confirmed':confirmed,'correction_version':correction.get('version',0),
            'movement_kind':kind,'sources':sorted(row_sources),'classification_reason':reason})
    result.sort(key=lambda r:(r['tx_date'],r['id']),reverse=True)
    revision=hashlib.sha256(json.dumps(result,sort_keys=True,default=str).encode()).hexdigest()
    page=result[request.offset:request.offset+request.limit]
    next_offset=request.offset+len(page) if request.offset+len(page)<len(result) else None
    return {'status':'available','items':page,'total':len(result),'next_offset':next_offset,'revision':revision,
        'period':[start.isoformat(),end.isoformat()],'categories':sorted(categories),'sources':sorted(sources),'notice':NOTICE}


def recording_intent(text: str) -> bool:
    return bool(re.search(r'\bкупил[аи]?\b|запиши.*расход',text))


def _legacy_answer(cfo: PersonalCFO, text: str) -> dict[str, Any] | None:
    """Route supported additional intents locally; never silently record money."""
    if any(word in text.casefold() for word in ('что важно сегодня', 'что требует внимания', 'сводка на сегодня')):
        from jarvis.finance.personal_briefing import daily_briefing
        brief = daily_briefing(cfo)
        return {'status': 'evidence_only', 'text': brief['text'], 'data': brief, 'paid_cost_rub': '0'}
    if recording_intent(text):
        return {'status':'clarification_required','text':'Это уже импортированный банковский платёж или отдельный наличный расход? Ничего не записано. Для исправления укажите ID операции; банковские суммы не меняются.', 'paid_cost_rub':'0'}
    if 'куп' in text and not re.search(r'\d',text):
        return {'status':'clarification_required','text':'Укажите цену покупки в рублях. Я рассчитаю сценарий с резервом, обязательствами и неизвестными данными; ничего не покупаю.', 'paid_cost_rub':'0'}
    if 'цел' in text:
        data = read_tool(cfo, ReadToolRequest(name='goals'))
        lines = [f"{g['name']}: {g['current_amount']} из {g['target_amount']} ₽; необходимый месячный взнос {(g.get('projection') or {}).get('required_monthly') or 'неизвестен'} ₽." for g in data['goals']]
        return {'status':'evidence_only' if lines else 'insufficient_data', 'text':'\n'.join(lines) if lines else 'Цели пока не заданы. Укажите сумму, уже накопленное и срок; без подтверждения цель не сохранится.', 'data':data,'paid_cost_rub':'0'}
    if any(word in text for word in ('доступн','свободн','можно потрат','сколько денег','сколько у меня')):
        position=read_tool(cfo,ReadToolRequest(name='position'))
        known=position.get('liquid');free=position.get('headroom')
        blocking=list(position.get('blocking_missing') or [])
        notes=list(position.get('missing') or [])
        if known is None or blocking or free is None:
            parts=['Ликвидные средства по известным данным: '+str(known)+' ₽.' if known is not None else 'Ликвидные средства неизвестны: нет свежих банковских остатков.']
            if blocking:parts.append('Не хватает: '+', '.join(blocking)+'.')
            parts.append('Свободную сумму не выдумываю и не подставляю ноль.')
            return {'status':'insufficient_data','text':' '.join(parts),'data':position,'paid_cost_rub':'0'}
        text=(f"Свободно к трате по известным данным: {free} ₽; ликвидные средства: {known} ₽; "
            f"резерв: {position.get('reserve_effective')} ₽; обязательства: {position.get('known_obligations_until_next_income')} ₽.")
        if notes:text+=' Учтено с допущениями: '+', '.join(notes)+'.'
        return {'status':'evidence_only','text':text+' Это расчёт по неполной истории, не гарантия.', 'data':position,'paid_cost_rub':'0'}
    if any(word in text for word in ('подписк','регулярн','списан')):
        data=read_tool(cfo,ReadToolRequest(name='patterns'))
        candidates=[p for p in data['patterns'] if p['kind']=='expense_candidate']
        if not candidates:
            return {'status':'insufficient_data','text':'Регулярных платежей-кандидатов нет. Это не доказывает отсутствие подписок: истории может быть мало.','data':data,'paid_cost_rub':'0'}
        lines=[];monthly=Decimal(0)
        for p in candidates[:8]:
            per_month=money(p['monthly_equivalent'])
            monthly+=per_month
            lines.append(f"{p['merchant']}: ≈{per_month.quantize(Decimal('.01'))} ₽/мес, следующее ожидаемое {p['next_expected_date']}.")
        return {'status':'review','text':'Кандидаты в регулярные платежи (не доказанные подписки): '+' '.join(lines)+f' Итого ≈{monthly.quantize(Decimal(".01"))} ₽/мес. Проверьте операции; автопродление не подтверждено.','data':data,'paid_cost_rub':'0'}
    if any(word in text for word in ('больше всего','крупные покупк','самые крупные','куда уходят','на что я трачу')):
        data=analytics(cfo);period=data['periods']['month']
        items=sorted(((key,money(value)) for key,value in period['by_category'].items()),key=lambda pair:pair[1],reverse=True)[:5]
        if not items:
            return {'status':'insufficient_data','text':'За текущий месяц нет известных расходов по категориям. Это не означает ноль: история может быть неполной.','data':data,'paid_cost_rub':'0'}
        lines=[f'{category_title(key)}: {value} ₽' for key,value in items]
        return {'status':'evidence_only','text':f"Расходы {period['period'][0]} — {period['period'][1]} по категориям: "+', '.join(lines)+'. Полнота истории и часть назначений могут быть неполными.','data':data,'paid_cost_rub':'0'}
    if any(word in text for word in ('чему ты научился','научил','что ты знаешь','сколько правил','обучен')):
        from jarvis.finance.classification.learning import provenance
        data=provenance(cfo)
        return {'status':'evidence_only','text':f"Подтверждено владельцем: {data['confirmed']}; отозвано: {data['withdrawn']}; допустимо для оценки: {data['eligible']}. Версия правил {str(data['revision'])[:12]}. Модель не продвигается автоматически; точность не измерена, пока меток мало.",'data':{key:data[key] for key in ('confirmed','withdrawn','eligible','revision','production_model_changed')},'paid_cost_rub':'0'}
    if any(word in text for word in ('нетипич', 'необыч', 'крупные трат', 'крупные расход')):
        data = read_tool(cfo, ReadToolRequest(name='patterns'))
        candidates = [p for p in data['patterns'] if p['kind'] == 'unusual_expense_candidate']
        lines = [f"Операция #{p['transaction_id']}: {p['amount']} ₽, {p['category']}." for p in candidates]
        return {'status':'review' if lines else 'insufficient_data', 'text':('Кандидаты для проверки: '+ ' '.join(lines) if lines else 'По доступной истории нет доказанного необычного расхода; наблюдений может быть недостаточно.')+' Это локальный статистический сигнал, не доказанная ошибка или лишняя покупка.', 'data':data,'paid_cost_rub':'0'}
    if any(word in text for word in ('накопить','накоплен','отклад')) and re.search(r'\d',text):
        match = re.search(r'(\d[\d ]*(?:[.,]\d{1,2})?)\s*(тыс(?:яч[аи]?)?)?',text)
        assert match is not None
        target = money(match[1].replace(' ', '').replace(',','.'))*(1000 if match[2] else 1)
        if not 0 < target <= 1_000_000_000:
            raise ValueError('Goal amount outside supported range')
        if 'полгода' in text or 'пол года' in text:
            weeks=26;weekly=(target/weeks).quantize(Decimal('.01'),rounding=ROUND_UP)
            message=f'С нуля до {target} ₽ за {weeks} недель: {weekly} ₽ в неделю, без процентов. Достижимость по вашим доходам не доказана; цель не сохранена.'
        elif 'год' in text:
            weekly=(target/52).quantize(Decimal('.01'),rounding=ROUND_UP)
            message=f'С нуля до {target} ₽ за 52 недели: {weekly} ₽ в неделю, без процентов. Достижимость по вашим доходам не доказана; резерв и обязательства должны быть известны. Цель не сохранена.'
        else:
            message=f'Для цели {target} ₽ нужны дедлайн и уже накопленная сумма. Не выбираю срок или взнос за вас; цель не сохранена.'
        return {'status':'scenario','text':message,'paid_cost_rub':'0'}
    if any(w in text for w in ('питан','продукт','недел','семь дней','7 дней','сегодня','месяц','потрат','расход','увелич','вырос','изменил','среднесуточ')):
        data=analytics(cfo)
        if 'питан' in text or 'продукт' in text:
            food=data['food'];message=f"Питание за месяц, известная часть: {food['month_spent']} ₽; среднее за календарный день {food['daily_known_average']} ₽. "
            message+=f"Остаток заданного бюджета: {food['remaining']} ₽; на день до конца месяца {food['daily_budget']} ₽. " if food['limit'] is not None else 'Лимит питания не задан; допустимую сумму не выдумываю. '
            message+=food['notice']
        elif 'увелич' in text or 'вырос' in text or 'изменил' in text:
            current, previous = ('month', 'previous_month') if 'месяц' in text else ('week', 'prior_week')
            if not all(data['periods'][key]['transaction_ids'] for key in (current, previous)):
                return {'status':'insufficient_data','text':'Недостаточно наблюдений в одном из сравниваемых периодов. Рост и его причина не доказаны; отсутствие записей не означает нулевые расходы.',
                    'data':data,'paid_cost_rub':'0'}
            a, b = data['periods'][current], data['periods'][previous]
            delta = money(a['expenses'])-money(b['expenses'])
            message=f"Период {a['period'][0]} — {a['period'][1]}: известные расходы {a['expenses']} ₽; период {b['period'][0]} — {b['period'][1]}: {b['expenses']} ₽; разность {delta} ₽. Причина не доказана, полнота и сопоставимость периодов неизвестны."
        else:
            key='today' if 'сегодня' in text else 'week' if any(w in text for w in ('недел','семь дней','7 дней')) else 'month'
            p=data['periods'][key];message=f"Период {p['period'][0]} — {p['period'][1]}: известные расходы {p['expenses']} ₽, поступления {p['income']} ₽; операций с неподтверждённым назначением {p['unresolved_count']}. {NOTICE}"
            if 'бюджет' in text:message+=' Без полного списка обязательств и резерва безопасный месячный лимит неизвестен. Лимиты категорий задаются отдельно.'
        return {'status':'evidence_only','text':message,'data':data,'paid_cost_rub':'0',
            'tool_trace':[{'name':'period','start':v['period'][0],'end':v['period'][1]} for v in data['periods'].values()]}
    return None


def answer(cfo: PersonalCFO, text: str) -> dict[str, Any] | None:
    """Keep legacy queries while routing natural questions through shared intelligence."""
    from jarvis.finance.personal_understanding import answer as understand
    return understand(cfo, text) or _legacy_answer(cfo, text)
