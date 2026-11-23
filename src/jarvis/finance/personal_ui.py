"""Install personal CFO tools in the existing authenticated review web."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from jarvis.finance.merchant_research import OrganizationSearch
from jarvis.finance.personal_cfo import PersonalCFO, PersonalPlan
from jarvis.finance.personal_product import RuntimeSettings
from jarvis.finance.personal_profile import GoalInput, PersonalProfile
from jarvis.finance.personal_queries import HistoryRequest, ReadToolRequest


class PurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price: str = Field(max_length=30)


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=80)
    expected_version: int | None = Field(default=None, ge=0)


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int | None = Field(default=None, ge=0)


class GroupItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: int = Field(gt=0)
    expected_version: int = Field(ge=0)
    input_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class GroupReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    group_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    items: list[GroupItem] = Field(min_length=1,max_length=100)
    category: str | None = Field(default=None,max_length=80)


class PinRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pin: str = Field(pattern=r'^\d{5}$')


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=50)


class ImportApply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    owner_confirmed: bool = False
    include_possible_duplicates: bool = False
    acknowledge_validation: bool = False


class CFOQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=1000)


INCOME_CATEGORY_KEYS = ('salary', 'stipend', 'scholarship', 'family_income',
    'family_support', 'refund', 'peer_reimbursement', 'parent_transfer_in',
    'sibling_transfer_in', 'friend_transfer_in', 'friend_reimbursement', 'other')

# Self-transfers make sense on both sides of the ledger; everything else is bound
# to the direction of the operation so the picker never offers a nonsense choice.
SELF_TRANSFER_KEYS = frozenset({'internal_transfer', 'savings_transfer', 'invest_kopilka', 'cash'})
ANY_DIRECTION_KEYS = frozenset({'peer_transfer'})


class CategoryLimit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category: str = Field(pattern=r'^[a-z_][a-z0-9_:]{0,79}$')
    monthly_limit: str = Field(max_length=25)


class ManualEntry(BaseModel):
    """Owner-typed cash movement; never a bank fact and never auto-categorized."""
    model_config = ConfigDict(extra='forbid')
    amount: str = Field(max_length=25)
    kind: Literal['expense', 'income'] = 'expense'
    category: str = Field(min_length=1, max_length=80)
    merchant: str = Field(default='', max_length=120)
    description: str = Field(default='', max_length=300)
    tx_date: date | None = None
    request_id: str = Field(pattern=r'^[a-f0-9]{8,64}$')


def question(cfo: PersonalCFO, message: str) -> dict[str, Any]:
    """Resolve the bounded purchase scenario without treating user balance as bank."""
    text = " ".join(message.casefold().split())
    from jarvis.finance.personal_queries import answer as query_answer
    from jarvis.finance.personal_queries import recording_intent
    if recording_intent(text):
        result = query_answer(cfo, text)
        assert result is not None
        return result
    price = re.search(r"(?:за|ценой|стоимостью)\s+(\d[\d ]*(?:[.,]\d{1,2})?)\s*(?:₽|руб)", text)
    if price and any(word in text for word in ("куп", "покуп", "озу", "памят")):
        return cfo.purchase(price[1].replace(" ", "").replace(",", "."))
    future_spend = re.search(r"(?:потрачу|потратить)\s+(\d[\d ]*(?:[.,]\d{1,2})?)\s*(?:₽|руб|[?!.]|$)", text)
    if future_spend:
        return cfo.purchase(future_spend[1].replace(' ', '').replace(',', '.'))
    additional = query_answer(cfo, text)
    if additional is not None:
        return additional
    data = cfo.dashboard()
    position = data["financial_position"]
    cash = cfo.profile().cash
    cash_description = "подтверждение владельца" if cash and cash.source == "owner_confirmed" else "оценка владельца" if cash else "не указаны"
    if any(w in text for w in ("не понимает", "непонятн", "неизвестные операции")):
        pending = [r for r in cfo.economic_rows(cfo.db.all_transactions()) if r['category'] in {'unknown', 'pending_review', 'uncategorized'}]
        return {'status': 'review', 'text': f'Во всей доступной истории {len(pending)} операций без понятной категории. Откройте историю или подготовьте локальные предложения; требуется проверка владельца.',
            'data': {'transaction_ids': [r['id'] for r in pending], 'scope': 'all_local_transactions'}, 'paid_cost_rub': '0'}
    if "хватит" in text or "до следующего" in text:
        until = position['until_next_income']['pessimistic']
        answer = ('Окончательный ответ неизвестен: ' + ', '.join(position['missing']) if position['missing'] or not position['next_income'] else
            f"До следующей датированной выплаты {position['next_income']} стресс-сценарий даёт минимум {until['minimum']} ₽ и запас после защиты {until['headroom']} ₽.")
        answer += ' Ожидаемое поступление не гарантировано; недатированные переводы не считаются обещанием.'
    elif ("сколько" in text and any(w in text for w in ("денег", "средств", "потратить", "останется"))) or "свободн" in text:
        answer = (f"Средства в наблюдаемом контуре: {position['total_low'] if position['total_low'] is not None else 'неизвестно'}–{position['total_high'] if position['total_high'] is not None else 'неизвестно'} ₽; "
            f"ликвидные по нижней оценке: {position['liquid'] if position['liquid'] is not None else 'неизвестно'} ₽. Наличные — {cash_description}, банк показан отдельно. "
            "Свободные средства после всех ограничений неизвестны. Не хватает: " + ", ".join(position["missing"]) + "." if position["missing"] else
            f"Условно свободно после защиты: {position['capacity']['scenarios']['keep_money']['headroom']} ₽. Ликвидные средства: {position['liquid']} ₽. Резерв: {position['reserve']} ₽; под цели: {position['goal_reserved']} ₽. Это сценарий, не гарантия полноты.")
    elif "отложить" in text or "откладыв" in text:
        from jarvis.finance.goals import build_goal_contribution_plan
        value = position["forecasts"][1]["variants"]["pessimistic"]["headroom"]
        if position["missing"] or value is None:
            answer = "Безопасная сумма накоплений пока неизвестна: " + ", ".join(position["missing"]) + "."
        else:
            plan_result = build_goal_contribution_plan(cfo.db, max(Decimal(0), Decimal(value)))
            answer = f"Условный максимум на неделю: {plan_result.available_amount} ₽. По существующим целям распределяется {plan_result.allocated_amount} ₽. Это план, переводов нет."
    elif "хватит" in text or "выплат" in text:
        answer = ("До ближайшего датированного поступления: " + str(position["next_income"]) + ". " +
            ("Окончательный ответ невозможен: " + ", ".join(position["missing"]) if position["missing"] else
             "Минимальный остаток в стресс-сценарии: " + str(position["until_next_income"]["pessimistic"]["minimum"]) + " ₽.") +
            " Недатированные поступления не учтены; ожидаемая выплата не гарантирована.")
    elif "подписк" in text or "сэконом" in text:
        candidates = [p for p in data["patterns"] if p["kind"] == "expense_candidate"]
        categories = sorted(data["cashflow"]["by_category"].items(), key=lambda pair: Decimal(pair[1]), reverse=True)[:3]
        answer = ("Для пересмотра: " + "; ".join(p["merchant"] + " — " + p["typical_net_expense"] + " ₽" for p in candidates)
            if candidates else "Доказанных лишних подписок нет; найденные повторения требуют проверки.")
        answer += " Крупные известные категории: " + "; ".join(k + " " + v + " ₽" for k, v in categories) + ". Экономия не обещается."
    elif "вырос" in text or "почему" in text:
        answer = "Причина роста расходов не доказана: покрытие частичное, сравнимые полные периоды и экономические категории не подтверждены. Можно проверить операции крупных категорий; корреляция не устанавливает причину."
    elif "потрат" in text or "месяц" in text:
        from jarvis.finance.ledger import cashflow
        today = datetime.now(UTC).date()
        rows = cfo.economic_rows(cfo.db.transactions_between(today.replace(day=1), today))
        flow = cashflow(rows)
        top = sorted(flow["by_category"].items(), key=lambda pair: pair[1], reverse=True)[:5]
        answer = "За текущий месяц, предварительно по известным категориям: " + "; ".join(k + " " + str(v) + " ₽" for k, v in top) + ". Покрытие неполное; собственные переводы исключены."
    else:
        answer = "Финансовая сводка ниже. Спросите о средствах, расходах, накоплениях, выплате или покупке с ценой."
    if position.get("reserve") is not None and Decimal(position["reserve"]) == 0:
        answer += " Резерв задан 0 ₽: запаса на неожиданные расходы нет; финансовые настройки не менялись."
    return {"status": position["status"], "text": answer, "data": position, "paid_cost_rub": "0"}


def install_personal_routes(app: FastAPI, cfo: PersonalCFO, authorized: Callable[[Request], None]) -> None:
    """Use the same loopback, origin and token boundary as review routes."""
    from threading import Lock
    pending_previews: dict[str, dict[str, Any]] = {}
    pending_lock = Lock()

    @app.get("/api/cfo/health", dependencies=[Depends(authorized)])
    def healthcheck() -> dict[str, Any]:
        from jarvis.finance.personal_recovery import health
        return health(cfo.db)

    @app.get('/api/cfo/ai-budget', dependencies=[Depends(authorized)])
    def ai_budget() -> dict[str, Any]:
        if getattr(app.state, 'web_only', False):
            return {'status':'disabled_web_only', 'dispatch_allowed':False, 'execution_allowed':False}
        from jarvis.core.llm.budget_store import BudgetStore
        from jarvis.core.llm.governance import canonical_journal
        store = BudgetStore(canonical_journal(), currency='RUB')
        if not store.path.is_file():
            return {'status': 'not_initialized', 'hard_monthly_limit_rub': '250', 'automatic_paid_search': False}
        try:
            return {'status': 'available', **store.global_report(now=datetime.now(UTC))}
        except RuntimeError:
            return {'status': 'unavailable', 'code': 'journal_unavailable',
                    'hard_monthly_limit_rub': '250', 'automatic_paid_search': False}

    @app.post('/api/cfo/merchant-research', dependencies=[Depends(authorized)])
    async def merchant_research(body: OrganizationSearch) -> dict[str, Any]:
        from jarvis.finance.merchant_research import research
        return await research(cfo, body)

    @app.post('/api/cfo/category/{tx_id}/propose', dependencies=[Depends(authorized)])
    async def propose_category(tx_id: int) -> dict[str, Any]:
        from jarvis.finance.classification.intelligence import propose, screened
        row = next((r for r in cfo.db.all_transactions() if r['id'] == tx_id), None)
        if row is None:
            return {'status': 'not_found'}
        with cfo.db.connect() as conn:
            manual = conn.execute("SELECT 1 FROM category_corrections c WHERE transaction_id=? AND source='manual' AND version=(SELECT MAX(c2.version) FROM category_corrections c2 WHERE c2.transaction_id=c.transaction_id)", (tx_id,)).fetchone()
        if manual:
            return {'status': 'owner_confirmed', 'category': row['category'], 'paid_cost_rub': '0'}
        structural = cfo.economic_rows([row])[0]
        if structural.get('category_origin') == 'bank_resource_proof':
            return {'status': 'bank_resource_proof', 'category': 'internal_transfer', 'paid_cost_rub': '0'}
        from jarvis.finance.classification.learning import (
            fingerprint,
            neighbor_proposal,
            provenance,
        )
        from jarvis.finance.classification.suggestions import rules_revision
        local = neighbor_proposal(cfo, tx_id)
        if local is not None:
            cfo.db.set_setting('cfo.ai_proposal:' + str(tx_id), local)
            return local
        prepared: dict[str, Any] = cfo.db.get_setting('cfo.ai_proposal:'+str(tx_id), {})
        candidate = prepared.get('result', {})
        if (candidate.get('suggestion_origin') == 'local_history_rules'
                and candidate.get('category') not in {None, 'unknown'}
                and candidate.get('rules_revision') == rules_revision()
                and candidate.get('source_revision') == provenance(cfo)['revision']
                and prepared.get('input_sha256') == fingerprint([row[k] for k in ('merchant','description','amount','currency')])):
            return prepared
        features = screened(row['description'], Decimal(row['amount']), cfo.confirmed_categories(row['merchant'], Decimal(row['amount'])))
        result = await propose(features)
        cfo.db.set_setting('cfo.ai_proposal:' + str(tx_id), result)
        return result

    @app.get("/api/cfo", dependencies=[Depends(authorized)])
    def dashboard() -> dict[str, Any]:
        return cfo.dashboard()

    @app.get('/api/cfo/briefing', dependencies=[Depends(authorized)])
    def briefing() -> dict[str, Any]:
        from jarvis.finance.personal_briefing import daily_briefing
        return daily_briefing(cfo)

    @app.get('/api/cfo/overview', dependencies=[Depends(authorized)])
    def product_overview(days: int = 30) -> dict[str, Any]:
        from jarvis.finance.personal_product import overview
        return overview(cfo, days=days)

    @app.post('/api/cfo/history', dependencies=[Depends(authorized)])
    def transaction_history(body: HistoryRequest) -> dict[str, Any]:
        from jarvis.finance.personal_queries import history
        return history(cfo,body)

    @app.get('/api/cfo/learning', dependencies=[Depends(authorized)])
    def learning_status() -> dict[str, Any]:
        from jarvis.finance.classification.learning import provenance
        data = provenance(cfo)
        return {key:value for key,value in data.items() if key!='examples'} | {'calibration':'not_measured','automatic_model_promotion':False}

    @app.get('/api/cfo/clusters', dependencies=[Depends(authorized)])
    def learning_clusters() -> dict[str, Any]:
        from jarvis.finance.classification.learning import clusters
        return clusters(cfo)

    @app.get('/api/cfo/proposal-groups', dependencies=[Depends(authorized)])
    def grouped_proposals() -> dict[str, Any]:
        from jarvis.finance.classification.learning import (
            label_plan,
            model_queue,
            onboarding_queue,
            proposal_groups,
        )
        from jarvis.finance.personal_queries import business_today
        cutoff=business_today()-timedelta(days=14)
        # The model queue fits a classifier; GET must not start training in Web-only.
        queue = ({'status':'disabled_web_only','notice':'Обучение очереди отключено в Web-only.'}
            if getattr(app.state, 'web_only', False) else model_queue(cfo))
        return {**proposal_groups(cfo),'label_plan':label_plan(cfo,cutoff),
            'model_queue':queue,'onboarding':onboarding_queue(cfo,cutoff=cutoff)}

    @app.post('/api/cfo/group-review', dependencies=[Depends(authorized)])
    def group_review(body: GroupReview) -> dict[str, Any]:
        from jarvis.finance.personal_product import review_batch
        try:
            return review_batch(cfo,body.group_id,[item.model_dump() for item in body.items],body.category)
        except ValueError as error:
            raise HTTPException(status_code=409,detail=str(error)) from error

    @app.post('/api/cfo/learning/prepare', dependencies=[Depends(authorized)])
    def prepare_local_history() -> dict[str, Any]:
        from jarvis.finance.classification.learning import prepare_history
        return prepare_history(cfo)

    @app.get('/api/cfo/analytics', dependencies=[Depends(authorized)])
    def analysis() -> dict[str, Any]:
        from jarvis.finance.personal_queries import analytics
        return analytics(cfo)

    @app.post('/api/cfo/search', dependencies=[Depends(authorized)])
    def search(body: SearchRequest) -> dict[str, Any]:
        from jarvis.finance.search import hybrid_search
        rows = hybrid_search(cfo.db.path, body.query, limit=body.limit)
        return {'status': 'ok', 'query': body.query, 'count': len(rows),
            'items': rows, 'financial_apply': False}

    @app.post('/api/cfo/tools', dependencies=[Depends(authorized)])
    def read_financial_tool(body: ReadToolRequest) -> dict[str, Any]:
        from jarvis.finance.personal_queries import read_tool
        return read_tool(cfo, body)

    @app.get('/api/cfo/runtime', dependencies=[Depends(authorized)])
    def runtime_state() -> dict[str, Any]:
        from jarvis.finance.personal_runtime import state
        result = state(cfo)
        build = getattr(app.state, 'runtime_build', None)
        if build:
            result['web_runtime'] = {'build':build, 'status':'running',
                'heartbeat_at':datetime.now(UTC).isoformat()}
            if build['no_jobs']:
                # Persisted job/poller state belongs to a previous process.
                result['runtime_active'] = False
                result['telegram'] = {'status':'disabled_web_only'}
        return result

    @app.put('/api/cfo/runtime', dependencies=[Depends(authorized)])
    def runtime_settings(body: RuntimeSettings) -> dict[str, Any]:
        cfo.db.set_setting('cfo.runtime_settings', body.model_dump())
        return {'status': 'saved'}

    @app.get('/api/cfo/similar/{tx_id}', dependencies=[Depends(authorized)])
    def similar_transactions(tx_id: int) -> dict[str, Any]:
        from jarvis.finance.personal_product import similar
        return {'status': 'review', 'items': similar(cfo.db, tx_id)}

    @app.post('/api/cfo/category/{tx_id}/undo', dependencies=[Depends(authorized)])
    def undo(tx_id: int, body: VersionRequest | None = None) -> dict[str, Any]:
        from jarvis.finance.personal_product import undo_correction
        return {'status': 'saved' if undo_correction(cfo.db, tx_id, body.expected_version if body else None) else 'not_applicable'}

    @app.put('/api/cfo/category-budget', dependencies=[Depends(authorized)])
    def category_budget(body: CategoryLimit) -> dict[str, Any]:
        from jarvis.finance.personal_cfo import money
        limit = money(body.monthly_limit)
        if limit < 0:
            raise ValueError('Бюджет не может быть отрицательным')
        with cfo.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute("SELECT value FROM settings WHERE key='cfo.category_limits'").fetchone()
            limits=json.loads(row[0]) if row else {}
            limits[body.category] = str(limit)
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ('cfo.category_limits',json.dumps(limits,allow_nan=False)))
        return {'status': 'saved'}

    @app.get("/api/cfo/data-status", dependencies=[Depends(authorized)])
    def status() -> dict[str, Any]:
        return cfo.data_status()

    @app.put("/api/cfo/profile", dependencies=[Depends(authorized)])
    def profile(body: PersonalProfile) -> dict[str, Any]:
        cfo.save_profile(body)
        return {"status": "saved", "profile": cfo.profile().model_dump(mode="json")}

    @app.post("/api/cfo/goals", dependencies=[Depends(authorized)])
    def goal(body: GoalInput) -> dict[str, Any]:
        existing = next((g for g in cfo.db.list_savings_goals(status=None) if g["name"] == body.name), None)
        goal_id = cfo.db.add_or_update_savings_goal(body.name, Decimal(body.target_amount), Decimal(body.current_amount),
            body.priority, body.deadline, notes=existing["notes"] if existing else "", status=body.status,
            metadata={"monthly_contribution": body.monthly_contribution,
            "updated_at": datetime.now(UTC).isoformat(), "source": "owner_input"})
        # With budget autopilot on, Qwen immediately aligns limits with the new goal.
        auto_plan = None
        try:
            from jarvis.finance.autopilot import (
                apply_budget_plan,
                budget_plan,
                load_settings,
            )
            if load_settings(cfo)["budget"]:
                plan = budget_plan(cfo, goal_id=goal_id)
                applied = apply_budget_plan(cfo, plan["categories"])
                auto_plan = {"applied": applied["applied"],
                             "planned_total": plan["planned_total"],
                             "months_to_goal": (plan.get("goal") or {}).get("months_to_goal")}
        except (OSError, ValueError, KeyError, TypeError):
            auto_plan = None
        return {"status": "saved", "goal_id": goal_id, "auto_plan": auto_plan}

    @app.get('/api/cfo/goals', dependencies=[Depends(authorized)])
    def goals() -> dict[str, Any]:
        from jarvis.finance.personal_profile import business_date
        return {'status': 'available', 'items': cfo.goals(business_date(datetime.now(UTC)))}

    @app.get('/api/cfo/categories', dependencies=[Depends(authorized)])
    def category_catalog() -> dict[str, Any]:
        from jarvis.finance.categories import CATEGORY_DEFS
        from jarvis.finance.event_taxonomy import CATEGORY_TITLES
        titles = {definition.key: definition.label for definition in CATEGORY_DEFS}
        titles.update(CATEGORY_TITLES)
        groups: dict[str, list[dict[str, str]]] = {}
        transfers: dict[str, list[dict[str, str]]] = {}
        income_groups: dict[str, list[dict[str, str]]] = {}
        categories: list[dict[str, str]] = []

        def direction(key: str, necessity: str) -> str:
            if key in SELF_TRANSFER_KEYS or key in ANY_DIRECTION_KEYS:
                return 'any'
            if necessity == 'income' or key in INCOME_CATEGORY_KEYS:
                return 'in'
            return 'out'

        for definition in CATEGORY_DEFS:
            if definition.necessity == 'unknown':
                continue
            item = {'category': definition.key, 'title': titles.get(definition.key, definition.label),
                    'bucket': definition.bucket,
                    'direction': direction(definition.key, definition.necessity)}
            categories.append(item)
            if item['direction'] == 'in':
                income_groups.setdefault(item['bucket'], []).append(item)
            elif definition.necessity == 'transfer':
                transfers.setdefault(item['bucket'], []).append(item)
            else:
                groups.setdefault(item['bucket'], []).append(item)
        known = {item['category'] for item in categories}
        for key in INCOME_CATEGORY_KEYS:
            if key in known:
                continue
            item = {'category': key, 'title': titles.get(key, key), 'bucket': 'Доходы',
                    'direction': 'in'}
            categories.append(item)
            income_groups.setdefault('Доходы', []).append(item)
        incomes = [item for item in categories if item['direction'] == 'in']
        return {'status': 'available', 'expense_groups': groups, 'transfer_groups': transfers,
            'income_groups': income_groups, 'categories': categories,
            'incomes': incomes, 'titles': titles,
            'limits': cfo.db.get_setting('cfo.category_limits', {}),
            'owner_editable': True, 'financial_apply': False}

    @app.post('/api/cfo/expenses', dependencies=[Depends(authorized)])
    def manual_entry(body: ManualEntry) -> dict[str, Any]:
        """Record an owner-typed cash movement once; bank facts stay untouched."""
        from jarvis.finance.domain import Transaction
        from jarvis.finance.event_taxonomy import (
            EXPENSE_CATEGORIES,
            NON_ECONOMIC_CATEGORIES,
            UNRESOLVED_CATEGORIES,
            canonical_category,
        )
        from jarvis.finance.personal_cfo import money
        from jarvis.finance.personal_product import validated_category
        from jarvis.finance.personal_profile import business_date
        try:
            amount = money(body.amount.replace(' ', '').replace(',', '.'))
        except (ValueError, ArithmeticError) as error:
            raise HTTPException(422, 'Сумма должна быть числом с копейками') from error
        if amount <= 0:
            raise HTTPException(422, 'Сумма должна быть больше нуля')
        if amount > Decimal(100000000):
            raise HTTPException(422, 'Слишком большая сумма для ручной записи')
        category = canonical_category(body.category.strip())
        if body.kind == 'expense':
            try:
                category = validated_category(category)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            if category in NON_ECONOMIC_CATEGORIES:
                raise HTTPException(422, 'Переводы и снятия не считаются расходом')
        elif category in EXPENSE_CATEGORIES or category in NON_ECONOMIC_CATEGORIES or category in UNRESOLVED_CATEGORIES:
            raise HTTPException(422, 'Выберите категорию поступления, а не расхода')
        today = business_date(datetime.now(UTC))
        tx_date = body.tx_date or today
        if tx_date > today:
            raise HTTPException(422, 'Дата операции не может быть в будущем')
        if tx_date < today - timedelta(days=3650):
            raise HTTPException(422, 'Дата операции слишком давняя')
        merchant = body.merchant.strip() or ('Наличные' if body.kind == 'expense' else 'Ручное поступление')
        description = body.description.strip() or ('Ручной расход' if body.kind == 'expense' else 'Ручное поступление')
        transaction = Transaction(tx_date=tx_date, amount=-amount if body.kind == 'expense' else amount,
            currency='RUB', description=description, merchant=merchant, category=category,
            account='cash', external_id='manual-' + body.request_id)
        tx_id = cfo.db.add_transaction(transaction, source='manual', parser_version='owner-web-1')
        return {'status': 'saved' if tx_id else 'duplicate', 'transaction_id': tx_id or None,
            'kind': body.kind, 'amount': str(transaction.amount), 'category': category,
            'tx_date': tx_date.isoformat(), 'financial_apply': True}

    @app.post('/api/cfo/backup', dependencies=[Depends(authorized)])
    def backup() -> dict[str, Any]:
        from jarvis.finance.personal_cfo import backup_database
        target = backup_database(cfo.db, cfo.db.path.parent / 'cfo-review' / 'backups')
        return {'status': 'verified', 'file': target.name,
            'created_at': datetime.now(UTC).isoformat(), 'financial_apply': False}

    @app.post("/api/cfo/refresh", dependencies=[Depends(authorized)])
    def refresh() -> dict[str, Any]:
        return cfo.refresh()

    @app.post('/api/cfo/import/preview', dependencies=[Depends(authorized)])
    async def import_preview(file: Annotated[UploadFile, File()],
                             account: Annotated[str, Form()] = 'sber') -> dict[str, Any]:
        from tempfile import NamedTemporaryFile

        from jarvis.finance.import_preview import preview_import
        suffix = Path(file.filename or '').suffix.lower()
        if suffix not in {'.csv', '.tsv', '.txt', '.xlsx', '.pdf'}:
            raise HTTPException(415, 'Поддерживаются CSV, TSV, TXT, XLSX и PDF с текстом')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', account):
            raise HTTPException(422, 'Некорректное имя счёта')
        data = await file.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(413, 'Файл больше 20 МБ')
        with NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
            temporary.write(data)
            path = Path(temporary.name)
        try:
            preview = preview_import(cfo.db, path, account=account)
        except Exception as error:
            raise HTTPException(400, 'Не удалось распознать выписку. Проверьте формат и колонки.') from error
        finally:
            path.unlink(missing_ok=True)
        preview_id = uuid.uuid4().hex
        preview['file_sha256'] = hashlib.sha256(data).hexdigest()
        with pending_lock:
            now = time.time()
            for key in [key for key, value in pending_previews.items() if value['expires'] < now]:
                pending_previews.pop(key, None)
            pending_previews[preview_id] = {'preview': preview, 'expires': now + 600}
        rows = [{'tx_date': tx.tx_date.isoformat(), 'amount': str(tx.amount), 'currency': tx.currency,
            'merchant': tx.merchant, 'description': tx.description, 'category': tx.category}
            for tx in preview['new_transactions']]
        return {'status': 'preview', 'preview_id': preview_id, 'new': preview['new'],
            'duplicate': preview['duplicate'], 'possible_duplicate': preview['possible_duplicate'],
            'warnings': preview['warnings'], 'rows': rows, 'account': account,
            'possible_rows': [{'tx_date': tx.tx_date.isoformat(), 'amount': str(tx.amount),
                'currency': tx.currency, 'merchant': tx.merchant, 'description': tx.description,
                'category': tx.category} for tx in preview['possible_transactions'][:200]],
            'account_identity': preview['account_identity'], 'identity_conflicts': preview['identity_conflicts'],
            'source_identity_conflicts': preview['source_identity_conflicts'],
            'validation': preview['validation'], 'can_import': preview['can_import'],
            'financial_apply': False, 'next': 'confirm_import'}

    @app.post('/api/cfo/import/apply', dependencies=[Depends(authorized)])
    def import_apply(body: ImportApply) -> dict[str, Any]:
        from jarvis.finance.import_preview import apply_preview
        if not body.owner_confirmed:
            raise HTTPException(403, 'Требуется подтверждение владельца')
        with pending_lock:
            entry = pending_previews.get(body.preview_id)
        if not entry or entry['expires'] < time.time():
            raise HTTPException(410, 'Предпросмотр истёк; загрузите выписку снова')
        result = apply_preview(cfo.db, entry['preview'], owner_confirmed=True,
            include_possible_duplicates=body.include_possible_duplicates,
            acknowledge_validation=body.acknowledge_validation)
        if result.get('status') != 'applied':
            reason = result.get('reason')
            if reason == 'validation_failed':
                raise HTTPException(409, 'Проверка выписки не пройдена; исправьте файл или обратитесь к отчёту проверки')
            if reason == 'validation_acknowledgement_required':
                raise HTTPException(409, 'Часть проверок недоступна; подтвердите импорт отдельно')
            raise HTTPException(409, 'Есть сомнительные дубли; подтвердите их отдельно')
        with pending_lock:
            pending_previews.pop(body.preview_id, None)
        return {'status': 'applied', 'inserted': result['inserted'],
            'next': 'group_review', 'financial_apply': True}

    @app.post('/api/cfo/submit-pin', dependencies=[Depends(authorized)])
    def submit_pin(body: PinRequest) -> Any:
        """Report the real bank/MCP state instead of blaming every failure on the PIN."""
        result = cfo.refresh(allow_pin=True, pin=body.pin)
        status = str(result.get('status') or '')
        code = str(result.get('code') or status)
        if status == 'updated':
            jobs = cfo.db.get_setting('cfo.jobs', {})
            sync = jobs.get('sync', {})
            if sync.get('status') == 'auth_required':
                now = datetime.now(UTC)
                jobs['sync'] = {**sync, 'status': 'updated', 'code': None, 'errors': 0,
                    'owner_action_required': False, 'at': now.isoformat(),
                    'next_at': (now+timedelta(minutes=30)).isoformat()}
                cfo.db.set_setting('cfo.jobs', jobs)
            return {'status': 'connected', 'bank_rub': cfo.dashboard().get('bank_rub')}
        retry = int(result.get('retry_after_seconds') or 0)
        if status == 'cooling_down':
            minutes = max(1, (retry + 59) // 60)
            return JSONResponse({'status': 'error', 'code': 'auth_cooldown',
                'retry_after_seconds': retry,
                'detail': f'Сбер ограничивает частые попытки входа. Повторите через {minutes} мин.'},
                status_code=429, headers={'Retry-After': str(retry)})
        if status == 'busy':
            return JSONResponse({'status': 'error', 'code': 'bank_busy',
                'detail': 'Синхронизация уже выполняется. Дождитесь завершения.'}, status_code=409)
        if code in {'sber_mcp_not_configured', 'sber_profile_invalid', 'not_configured'}:
            return JSONResponse({'status': 'error', 'code': code,
                'detail': 'Sber MCP не настроен на этом ПК: проверьте SBER_MCP_HOME, профиль и команду MCP.'},
                status_code=503)
        if code in {'pin_format_invalid'}:
            return JSONResponse({'status': 'error', 'code': code,
                'detail': 'PIN должен состоять ровно из 5 цифр.'}, status_code=422)
        if code in {'pin_delivery_missing', 'pin_delivery_not_private'}:
            return JSONResponse({'status': 'error', 'code': code,
                'detail': 'MCP не выдал приватный канал для PIN. Вход нужно повторить через локальный launcher Сбера.'},
                status_code=409)
        if result.get('awaiting_secret') or code in {'pin_required', 'awaiting_pin', 'awaiting_secret', 'challenge'}:
            return JSONResponse({'status': 'error', 'code': code, 'awaiting_secret': True,
                'detail': 'PIN принят локально, но банк ещё не подтвердил вход. Подождите минуту и обновите Сбер; '
                          'если повторится — нужен локальный вход через launcher.'}, status_code=409)
        if code == 'history_scope_invalid':
            return JSONResponse({'status': 'error', 'code': code, 'auth_ok': True,
                'detail': 'Вход в Сбер выполнен, но история не собрана: профиль MCP не совпал. '
                          'Проверьте SBER_MCP_PROFILE (сейчас должен быть default).'},
                status_code=409)
        if code in {'ingest_rejected', 'capture_failed', 'history_unavailable'}:
            reason = str(result.get('reason') or result.get('stage') or result.get('detail')
                         or result.get('source_error') or '')
            reason = ''.join(ch for ch in reason if ch.isalnum() or ch in '_:. -')[:80]
            return JSONResponse({'status': 'error', 'code': code, 'auth_ok': True, 'reason': reason,
                'detail': f'Вход выполнен, но данные Сбера не приняты ({reason or "без деталей"}). '
                          'Сохранённый снимок не заменён.'}, status_code=409)
        if code in {'pin_invalid', 'invalid_pin', 'auth_rejected', 'pin_rejected'}:
            return JSONResponse({'status': 'error', 'code': code,
                'detail': 'Банк отклонил PIN. Проверьте код и попробуйте снова.'}, status_code=401)
        if status == 'unavailable' or code in {'MCPUnavailable', 'transport_failed', 'OSError', 'ValueError',
                                               'OperatorBlocked', 'MCPError'}:
            return JSONResponse({'status': 'error', 'code': code,
                'detail': 'Банковский MCP недоступен (сеть или запуск), PIN не проверялся. '
                          'Проверьте соединение и повторите.'}, status_code=502)
        return JSONResponse({'status': 'error', 'code': code,
            'detail': f'Вход не завершён (код {code}). Подробности — в журнале сервиса.'}, status_code=409)

    @app.put("/api/cfo/plan", dependencies=[Depends(authorized)])
    def plan(body: PersonalPlan) -> dict[str, str]:
        cfo.save_plan(body)
        return {"status": "saved", "source": "owner_input"}

    @app.post("/api/cfo/purchase", dependencies=[Depends(authorized)])
    def purchase(body: PurchaseRequest) -> dict[str, Any]:
        # Web-only mode must not open a bank session behind an owner click.
        if not getattr(app.state, 'web_only', False):
            cfo.ensure_bank_current()
        return cfo.purchase(body.price)

    @app.post("/api/cfo/question", dependencies=[Depends(authorized)])
    def ask(body: CFOQuestion) -> dict[str, Any]:
        return question(cfo, body.message)

    @app.put("/api/cfo/category/{tx_id}", dependencies=[Depends(authorized)])
    def correct(tx_id: int, body: CorrectionRequest) -> dict[str, Any]:
        return {"status": "saved" if cfo.correct(tx_id, body.category, body.expected_version) else "conflict_or_missing"}

    @app.post("/api/cfo/explain", dependencies=[Depends(authorized)])
    async def explain() -> dict[str, Any]:
        from jarvis.core.llm.orchestrator import complete_personal_analysis
        return await complete_personal_analysis(cfo, cfo.db.path.parent / "cfo-ai",
            monthly_limit="250")
