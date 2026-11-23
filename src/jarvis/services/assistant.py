"""Private local-first conversation over existing finance, memory and knowledge."""
from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_store import ReviewStore
from jarvis.knowledge.retriever import KnowledgeRetriever
from jarvis.services.chat_scenarios import ChatScenario, calculate_scenario
from jarvis.services.russian_scenarios import russian_scenario


def validate_chat_identity(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Conversation identifiers must be 32 hex characters")
    return value


class AssistantService:
    """Route before optional public cloud transmission; never apply money or labels."""

    def __init__(self, store: ReviewStore, *, ledger: Path | None = None,
                 accepted_sources: frozenset[str] = frozenset(), accepted_sha256: str | None = None,
                 knowledge: KnowledgeRetriever | None = None, public_sources: frozenset[str] = frozenset(),
                 budget: Path | None = None, prices: Path | None = None, transport: httpx.MockTransport | None = None,
                 personal_cfo: Any = None) -> None:
        self.store, self.ledger, self.accepted_sources = store, ledger, accepted_sources
        self.accepted_sha256, self.knowledge, self.public_sources = accepted_sha256, knowledge, public_sources
        self.budget, self.prices = budget, prices
        self.transport = transport
        self.personal_cfo = personal_cfo

    def cognitive_conversation(self) -> Any:
        """Share this assistant's journal and existing financial service."""
        from jarvis.services.cognitive import CognitiveConversation
        if self.personal_cfo is None:raise ValueError('Local financial service required')
        return CognitiveConversation(self.store,self.personal_cfo)

    def local_analysis(self, month: str, currency: str, now: datetime) -> dict[str, Any]:
        """Explain the accepted statement subset without cloud or completeness guesses."""
        from jarvis.finance.statement_product import StatementProduct
        try:
            report = StatementProduct(self.store).month_report(month, currency, now=now)
        except (ValueError, OSError):
            return self.answer("insufficient_data", "LOCAL_AUTOPILOT",
                "Нет принятого источника для выбранного периода и валюты. Подготовьте выписку и проверьте её scope.", current=False)
        lines = ["Локальный анализ · " + " — ".join(report["period"]) + " · " + currency,
            "Подтверждённая часть экономических расходов: " + report["known_expenses"] + " " + currency,
            "Списания без подтверждённой экономической категории: " + report["unknown_debits"] + " " + currency,
            "Поступления без подтверждённой экономической категории: " + report["unknown_credits"] + " " + currency,
            f"Покрыто счетов: {report['covered_accounts']} из {report['known_accounts']}; требуют категории: {report['unresolved_categories']}.",
            "Переводы не приравниваются к расходам или заработку. Числа рассчитаны Decimal.", report["balance_notice"]]
        if not report["coverage_complete"]:
            lines.append("Покрытие неполное: это только известная часть, не общая финансовая картина.")
        lines.append("Подписки и причины изменения расходов пока не подтверждены; нужны сопоставимая история и категории.")
        return self.answer("evidence_only", "LOCAL_AUTOPILOT", "\n".join(lines), data=report,
            source_revisions=[report["version"], report["source_sha256"]], current=True,
            limitations=["Принятые исторические выписки; текущие остатки и полнота всех счетов неизвестны."])

    async def ask(self, message: str, *, conversation_id: str, request_id: str,
                  month: str | None = None, currency: str = "RUB", public_cloud: bool = False,
                  now: datetime | None = None, explain: bool = False, scenario: ChatScenario | None = None,
                  statement_only: bool = False) -> dict[str, Any]:
        """Persist a bounded private turn and return a typed source-linked answer."""
        validate_chat_identity(conversation_id)
        validate_chat_identity(request_id)
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 2000:
            raise ValueError("Bounded nonempty conversation required")
        if re.search(r"(?i)\b(pin|otp|cookie|authorization|password|пин|пароль|токен)\b|sk-[\w-]+|(?:\d[ -]?){16,20}", message):
            raise ValueError("Не вводите секреты или полные банковские идентификаторы в чат")
        clock = now or datetime.now(UTC)
        if clock.tzinfo is None:
            raise ValueError("Aware conversation clock required")
        period = month or (clock.date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        body = {"message": message, "month": period, "currency": currency, "public_cloud": public_cloud,
                "ledger_scope": self.accepted_sha256, "sources": sorted(self.public_sources), "explain": explain}
        if statement_only:
            body['statement_only'] = True
        if scenario is not None:
            body["scenario"] = scenario.model_dump(mode="json")
        if not self.store.claim_chat(conversation_id, request_id, body):
            return self.answer("unavailable", "UNAVAILABLE", "Этот запрос уже сохранён или ещё выполняется. Нового вызова нет; история не является актуальным расчётом.", current=False, cached=True)
        try:
            if self.personal_cfo is not None and not statement_only and scenario is None:
                from jarvis.finance.personal_ui import question
                value = await asyncio.to_thread(question, self.personal_cfo, message)
                result = self.answer(value["status"], "LOCAL_PERSONAL_CFO", value["text"], data=value,
                    paid_cost_rub="0", current=value["status"] != "insufficient_data", production_database_modified=False,
                    storage_scope="local_chat_journal_only; financial_ledger_read_only")
                if explain:
                    from jarvis.core.llm.orchestrator import complete_personal_analysis
                    try:
                        insight = await complete_personal_analysis(self.personal_cfo, self.personal_cfo.db.path.parent / 'cfo-ai')
                    except Exception as exc:  # noqa: BLE001 - keep local facts; never expose provider errors or retry
                        insight = {'status': 'unavailable', 'paid_cost_rub': None,
                            'cost_status': 'unknown', 'automatic_retry': False, 'error_type': type(exc).__name__,
                            'text': 'Облачное пояснение недоступно. Локальные факты сохранены; стоимость обращения неизвестна, автоматического повтора нет.'}
                    result['ai_explanation'] = insight
                    result['paid_cost_rub'] = insight.get('paid_cost_rub')
                    if insight.get('text'):
                        result['text'] += '\n' + insight['text']
            elif statement_only:
                statement_result = self.statement_finance(message, self.store.chat_history(conversation_id), clock, month=month, currency=currency)
                if statement_result is None:
                    result = self.answer('insufficient_data', 'LOCAL_FINANCE', 'В этом режиме доступны только финансовые вопросы по принятым выпискам; укажите вопрос и даты.', current=False)
                else:
                    result = statement_result
            elif scenario is not None:
                data = calculate_scenario(self.store, scenario, now=clock)
                text = "ГИПОТЕТИЧЕСКИЙ расчёт; это не текущий остаток, принятый источник или сохранённая цель." if scenario.mode != "accepted_capacity" else "Сценарий по отдельно принятым источникам; будущий доход и ежемесячная норма накоплений неизвестны."
                result = self.answer(data["status"], "LOCAL_SCENARIO", text, data=data, confirmed=False,
                    source_revisions=[data.get("accepted_evidence_sha256")], navigation="#capacity")
            else:
                turns = self.store.chat_history(conversation_id)
                parsed = russian_scenario(message, turns, clock)
                if parsed is not None:
                    if "clarification" in parsed:
                        result = self.answer("insufficient_data", "LOCAL_SCENARIO", parsed["clarification"], current=False)
                    else:
                        data = calculate_scenario(self.store, parsed["scenario"], now=clock)
                        result = self.answer(data["status"], "LOCAL_SCENARIO", "Гипотетический расчёт по вашим параметрам; начало — дата текущего расчёта. Реальные деньги не изменены.", data=data)
                    result.update(nl_parameters=parsed["parameters"], nl_observed_at=clock.isoformat())
                else:
                    followup = self.statement_finance(message, turns, clock, month=month, currency=currency)
                    if followup is None:
                        followup = self.continue_finance(message, turns, clock)
                    result = followup if followup is not None else await self.route(message, period, currency, public_cloud, clock, explain=explain)
        except (ValueError, sqlite3.Error, OSError):
            result = self.answer("unavailable", "UNAVAILABLE", "Источник или выбранный период недоступен. Проверьте настройки и принятые данные.", current=False)
        if self.budget is not None and (result["route"].startswith("LOCAL_") or result.get("cloud_status") == "not_attempted_local_first"):
            from jarvis.core.config import settings
            from jarvis.core.llm.budget_store import BudgetStore, BudgetStoreUnavailable
            try:
                BudgetStore(self.budget, currency="RUB", simulation=settings.llm_mode == "mock").observe("local_only", event_id=request_id)
            except BudgetStoreUnavailable:
                result["telemetry_status"] = "unavailable"
        result.update(conversation_id=conversation_id, request_id=request_id)
        self.store.finish_chat(request_id, result)
        return result

    def statement_finance(self, message: str, turns: list[dict[str, Any]], now: datetime, *,
                          month: str | None, currency: str) -> dict[str, Any] | None:
        """Resolve bounded Russian scopes into the existing financial-question contract."""
        from jarvis.finance.audit_report import (
            AcceptedStatementScope,
            financial_question,
        )
        query = ' '.join(message.casefold().strip().rstrip('?.!').split())
        registered = any(k.startswith('statement:') for k in self.store.metadata())
        prior = None
        for turn in reversed(turns):
            candidate = (turn.get('answer') or {}).get('data') or {}
            if candidate.get('scope') == 'accepted_statements_only':
                prior = candidate
                break
        if self.ledger is not None and not registered and prior is None:
            return None
        if 'мои четыре счета' in query or re.search(r'\bистори[яю] (?:по выпискам|счетов|банковского движения)\b', query):
            from jarvis.finance.statement_product import StatementProduct
            history = StatementProduct(self.store).history(asof=now.date())
            lines = ['История по принятым выпискам: ' + str(history['accepted_sources']) + ' источника, '
                     + str(history['observations']) + ' наблюдений.']
            for source in history['source_scopes']:
                lines.append(source['account_alias'] + ' · ' + ' — '.join(source['period']) + ': пополнения '
                    + source['credits'] + ', списания ' + source['debits'] + ', движение ' + source['signed_movement']
                    + ' ' + source['currency'] + '; исторический closing на ' + source['closing_at'] + ': ' + source['closing'])
            lines.append(history['notice'] + ' Текущие средства и инвестиционная capacity неизвестны. '
                         'Полный календарный месяц проверяется отдельно для каждого счёта; категории требуют проверки.')
            return self.answer('available', 'LOCAL_FINANCE', '\n'.join(lines), data=history,
                source_revisions=[history['binding']], confirmed=False, historical=True,
                limitations=['Historical accepted PDF facts; not current funds or complete economic income/expenses.'])
        changed = 'изменил' in query or 'изменилось' in query or 'правки категории' in query
        intent = ('available_funds' if any(w in query for w in ('сейчас потратить', 'свободных средств', 'свободные средства')) else
                  'food' if any(w in query for w in ('продукт', 'к еде', 'на еду')) else
                  'income' if any(w in query for w in ('заработал', 'заработок', 'экономические доходы')) else
                  'bank_movement' if any(w in query for w in ('движение денег', 'прошло по', 'приток', 'отток', 'движение по')) else
                  'spending' if 'потратил' in query else None)
        if changed:
            intent = prior.get('intent') if prior else None
        if intent is None:
            return None
        iso = re.search(r'с (\d{4}-\d{2}-\d{2}) по (\d{4}-\d{2}-\d{2})', query)
        names = {'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
                 'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12}
        russian = re.search(r'с (\d{1,2}) (' + '|'.join(names) + r') по (\d{1,2}) (' + '|'.join(names) + r') (\d{4})(?: года)?', query)
        explicit_currency = re.search(r'\b(RUB|USD|EUR)\b', message.upper())
        chosen_currency = explicit_currency[1] if explicit_currency else prior['currency'] if prior else currency
        accounts = None
        if iso:
            start, end = date.fromisoformat(iso[1]), date.fromisoformat(iso[2])
        elif russian:
            year = int(russian[5])
            start, end = date(year, names[russian[2]], int(russian[1])), date(year, names[russian[4]], int(russian[3]))
        elif month:
            start = date.fromisoformat(month + '-01')
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        elif prior:
            start, end = map(date.fromisoformat, prior['period'])
            accounts = tuple(prior['account_scope']) if prior.get('account_scope') else None
        elif intent == 'available_funds':
            return self.answer('insufficient_data', 'LOCAL_FINANCE', 'Точные свободные средства неизвестны: нужны свежие принятые остатки, резерв и обязательства. Укажите суммы для гипотетического сценария.', current=False)
        else:
            return self.answer('insufficient_data', 'LOCAL_FINANCE', 'Укажите точные даты: «Покажи движение денег с 14 сентября по 9 октября 2026 года». Старые ответы не заменяют принятый источник.', current=False)
        selected_account = re.search(r'по (карте|сч[её]ту)\s*[•*]*(\d{4})\b', query)
        if (re.search(r'по (карте|сч[её]ту)\b', query) and selected_account is None
                or len(re.findall(r'(?:карте|сч[её]ту)\s*[•*]*\d{4}\b', query)) > 1):
            return self.answer('insufficient_data', 'LOCAL_FINANCE', 'Укажите один однозначный счёт/карту по последним четырём цифрам или используйте явный account scope API.', current=False)
        if selected_account:
            from jarvis.finance.statement_product import StatementProduct
            candidates = {s['metadata']['local_account'] for s in StatementProduct(self.store).sources()
                if (re.sub(r'\D', '', str(s['metadata'].get('card') or ''))[-4:] if selected_account[1] == 'карте'
                    else str(s['metadata']['bank_account'])[-4:]) == selected_account[2]}
            if len(candidates) != 1:
                return self.answer('insufficient_data', 'LOCAL_FINANCE', 'Счёт по последним четырём цифрам не определён однозначно; проверьте mapping в источниках.', current=False)
            accounts = tuple(sorted(candidates))
        scope = AcceptedStatementScope(start=start, end=end, accounts=accounts)
        data = financial_question(None, intent=intent, currency=chosen_currency, statement_scope=scope,
                                  review_store=self.store, now=now)
        text = self.statement_text(data)
        if changed and prior and data.get('snapshot_accepted'):
            data['previous_snapshot_version'] = prior.get('snapshot_version')
            data['revision_changed'] = prior.get('snapshot_version') != data['snapshot_version']
            text = ('Пересчитано по текущей версии после изменения источника/метки. ' if data['revision_changed'] else 'Изменения версии источника/меток не обнаружены. ') + text
        return self.answer(data['status'], 'LOCAL_FINANCE', text, data=data,
            confirmed=data['status'] == 'available', current=bool(data.get('snapshot_accepted')),
            source_revisions=[data.get('source_sha256'), data.get('snapshot_version')],
            limitations=['Только явно принятые выписки выбранного периода; не полная финансовая жизнь.'], navigation='#sources')

    @staticmethod
    def statement_text(data: dict[str, Any]) -> str:
        """Describe deterministic totals; no merchant text, account IDs or cloud synthesis."""
        if not data.get('snapshot_accepted'):
            return str(data.get('reason') or 'Источник недоступен; старые суммы не используются.')
        period = ' — '.join(data['period'])
        prefix = f"Период {period}, {data['currency']}. Только принятые выписки. "
        if data['intent'] == 'available_funds':
            return prefix + str(data['reason'])
        flow = data.get('bank_movements')
        if data['intent'] == 'bank_movement':
            text = ('Подтверждённое банковское движение: ' if data['coverage_complete'] else 'Неполная выборка банковского движения: ')
            text += f"приток {flow['credits']}, отток {flow['debits']}, чистое движение {flow['net']}." if flow else 'значения неизвестны.'
            text += ' Поступления включают переводы и не означают заработок.'
        elif data['intent'] == 'income':
            text = (f"Подтверждённые экономические доходы: {data['answer']}." if data['answer'] is not None else
                    'Полный заработок не подтверждён. Поступления на счёт нельзя считать зарплатой или прибылью.')
        elif data['intent'] == 'food':
            text = f"Подтверждённая часть категории «продукты»: {data['known_category_total']}. Категории: food/food_basic; кафе и доставка отдельно." if data['known_category_total'] is not None else 'Нет подтверждённого источника выбранной валюты; сумма продуктов неизвестна.'
            if data['answer'] is None:
                text += ' Полная сумма продуктов неизвестна.'
            for row in data['category_operations'][:5]:
                text += f"\n{row['date']}: {row['amount']} {row['currency']}."
        else:
            text = f"Подтверждённые экономические расходы: {data['answer']}." if data['answer'] is not None else 'Полные экономические расходы неизвестны.'
        text += f" Покрыто счетов {data['covered_accounts']}/{data['known_accounts']}; непроверенных категорий {data['unresolved_categories']}; списаний с неясной семантикой {data['unknown_debits']}, поступлений {data['unknown_credits']}."
        return prefix + text + ' Исторические остатки не подтверждают текущие свободные средства.'

    def continue_finance(self, message: str, turns: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
        """Recompute narrowly scoped financial followups; never forward history to cloud."""
        query = " ".join(message.casefold().rstrip("?.!").split())
        months = {name: n for n, name in enumerate(("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"), 1)}
        chosen = next((n for name, n in months.items() if name in query), None)
        comparison = query in {"какая разница", "а какая разница", "сравни эти месяцы"}
        if not comparison and not (query.startswith("а за ") and chosen):
            return None
        prior = [t["answer"] for t in turns if t.get("answer")]
        if not prior or prior[-1]["route"] != "LOCAL_FINANCE" or self.ledger is None:
            return self.answer("insufficient_data", "LOCAL_FINANCE", "Неоднозначный или истёкший контекст. Укажите месяц, валюту и финансовый вопрос явно.", current=False)
        from jarvis.finance.audit_report import financial_question
        data = prior[-1].get("data", {})
        if data.get("intent") != "spending" or not data.get("period"):
            return self.answer("insufficient_data", "LOCAL_FINANCE", "Этот follow-up требует явного контекста расходов; прежние суммы не используются.", current=False)
        currency = data["currency"]
        if not comparison:
            period = data["period"][0][:4] + f"-{chosen:02d}"
            fresh = financial_question(self.ledger, month=period, intent="spending", currency=currency,
                accepted_sources=self.accepted_sources, accepted_sha256=self.accepted_sha256, now=now)
            return self.answer(fresh["status"], "LOCAL_FINANCE", f"Период {period}: {fresh['answer']} {currency}." if fresh["status"] == "available" else "Недостаточно текущих принятых источников; старая сумма не используется.",
                data=fresh, current=fresh["status"] == "available", confirmed=fresh["status"] == "available", source_revisions=[fresh.get("source_sha256")])
        if len(prior) < 2 or prior[-2]["route"] != "LOCAL_FINANCE":
            return self.answer("insufficient_data", "LOCAL_FINANCE", "Нужны два явно выбранных месяца расходов.", current=False)
        previous = prior[-2].get("data", {})
        if previous.get("intent") != "spending" or previous.get("currency") != currency or not previous.get("period"):
            return self.answer("insufficient_data", "LOCAL_FINANCE", "Нельзя сравнивать разные валюты или неоднозначные расчёты.", current=False)
        periods = [previous["period"][0][:7], data["period"][0][:7]]
        fresh_rows = [financial_question(self.ledger, month=p, intent="spending", currency=currency,
            accepted_sources=self.accepted_sources, accepted_sha256=self.accepted_sha256, now=now) for p in periods]
        if any(r["status"] != "available" for r in fresh_rows) or len({r["source_sha256"] for r in fresh_rows}) != 1:
            return self.answer("unavailable", "LOCAL_FINANCE", "Источники изменились или покрытие неполно. Исторические суммы не используются для сравнения.", current=False)
        difference = Decimal(fresh_rows[1]["answer"]) - Decimal(fresh_rows[0]["answer"])
        return self.answer("available", "LOCAL_FINANCE", f"Расходы {periods[1]} минус {periods[0]}: {difference} {currency}. Это разница, не объяснение причин.",
            confirmed=True, data={"intent": "month_comparison", "periods": periods, "currency": currency, "difference": str(difference), "recomputed": fresh_rows}, source_revisions=[fresh_rows[0]["source_sha256"]])

    @staticmethod
    def answer(status: str, route: str, text: str | None, **extra: Any) -> dict[str, Any]:
        return {"status": status, "route": route, "text": text, "answer_kind": route.casefold(),
                "generated": False, "confirmed": False, "current": True, "citations": [],
                "source_revisions": [], "limitations": [], "model": None, "cost_rub": "0",
                "cached": False, "requires_human_action": status not in {"available", "evidence_only"},
                "production_database_modified": False, "financial_apply_allowed": False, **extra}

    async def route(self, message: str, month: str, currency: str, cloud: bool, now: datetime, *, explain: bool = False) -> dict[str, Any]:
        query = " ".join(message.casefold().strip().rstrip("?!.").split())
        if any(word in query for word in ("накоп", "достигну цели", "хватит ли", "расходы увелич", "доход задерж")):
            return self.answer("insufficient_data", "LOCAL_SCENARIO", "Откройте параметры сценария: гипотетические суммы задаются явно; текущая capacity требует четырёх отдельно принятых актуальных источников. LLM не считает деньги.", current=False, navigation="#capacity")
        if query in {"привет", "привет jarvis", "привет, jarvis", "что ты умеешь", "привет, jarvis. расскажи, что ты умеешь"}:
            return self.answer("available", "LOCAL_HELP", "Привет! Я показываю принятую историю расходов, очередь проверки, память и состояние источников. Объясняю публичные знания по разрешённым документам. Финансовые данные остаются локальными; облако по умолчанию выключено.")
        if any(word in query for word in ("проверки", "подтвержден", "подтверждён", "проверить операции")):
            items = self.store.items()
            pending = [i for i in items if i["status"] == "pending_review"]
            return self.answer("available", "LOCAL_REVIEW", f"Ожидают проверки: {len(pending)}. Подтверждение категории не является принятием банковских сумм.", review_ids=[i["id"] for i in pending[:20]], navigation="#status", source_revisions=[self.store.metadata()["packet_hash"]])
        if any(word in query for word in ("помнишь", "памят", "целях", "исправлял")):
            memory = HumanLabels(self.store).memory(now=now)
            notes = [n["text"] for n in memory["preferences"] if n["status"] == "nonfinancial_note"]
            return self.answer("available", "LOCAL_MEMORY", "Подтверждённых категорий: " + str(len(memory["facts"])) + ". Предпочтения: " + ("; ".join(notes) or "не сохранены") + ". Подтверждённая категория не подтверждает финансовый источник.", source_revisions=[memory["snapshot_sha256"]], data=memory)
        if any(word in query for word in ("сбер", "источник", "устарел", "свеж")):
            from jarvis.finance.review_availability import source_availability
            data = source_availability(self.store)
            states = "; ".join(f"{name}: {entry['status']}" for name, entry in data["sources"].items())
            return self.answer(data["status"], "LOCAL_SOURCE_STATUS", "Сохранённые статусы: " + states + ". Live-проверка не выполнялась; наличие ключей не доказывает доступ.", data=data, navigation="#availability")
        private = bool(re.search(r"\b(мой|моя|мои|моих|моим|моими|моих|мне|меня|my|mine)\b", query))
        finance = any(w in query for w in ("потрат", "расход", "отклады", "свободных", "регулярн", "подписк", "финансами", "денег", "баланс", "счета", "счетах", "счету", "счёте"))
        if private and not finance:
            return self.answer("insufficient_data", "LOCAL_MEMORY", "Этот личный факт не подтверждён доступной памятью. Не буду выводить его из финансовых операций или отправлять вопрос в облако.", current=False)
        if finance or private:
            from jarvis.finance.audit_report import financial_question
            intent = "category_growth" if "почему" in query or "вырос" in query else "expense_history" if "категор" in query or "больше всего" in query else "recurring" if "регулярн" in query or "подписк" in query else "available_funds" if any(w in query for w in ("отклады", "свободн", "денег", "баланс", "счет", "счёт")) else "missing_data" if "финансами" in query else "spending"
            if self.ledger is None:
                return self.answer("insufficient_data", "LOCAL_FINANCE", "Нет принятого снимка учёта. Нужны подтверждённое покрытие счетов, источники и сверка; неизвестные суммы не заменяются нулями.", current=False)
            if "прошл" in query:
                month = (now.date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            else:
                names = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
                selected = next((n for n, name in enumerate(names, 1) if name in query), None)
                if selected:
                    month = month[:4] + f"-{selected:02d}"
            result = financial_question(self.ledger, month=month, intent=intent, currency=currency,
                accepted_sources=self.accepted_sources, accepted_sha256=self.accepted_sha256, now=now)
            text = "Недостаточно принятых данных для достоверного ответа. Требуется сверка и актуальный источник."
            if result["status"] == "available" and result["answer"] is not None:
                text = f"Период: {month}, валюта: {currency}. " + (f"Принятые расходы за вычетом возвратов: {result['answer']} {currency}. Переводы и капитал исключены." if intent == "spending" else result["explanation"])
            return self.answer(result["status"], "LOCAL_FINANCE", text, confirmed=result["status"] == "available", current=result["status"] == "available", data=result, source_revisions=[result.get("source_sha256"), result.get("calculation_id")])
        if any(w in query for w in ("что такое", "объясни", "инфляц", "дюрац", "облигац", "резерв", "подушк")):
            topics = {"резерв": "финансовый резерв", "подушк": "финансовый резерв", "инфляц": "инфляция",
                      "дюрац": "дюрация", "облигац": "облигация", "ликвид": "ликвидность", "купон": "купон облигации"}
            topic = next((name for fragment, name in topics.items() if fragment in query), None)
            if topic is None:
                return self.answer("insufficient_data", "PUBLIC_KNOWLEDGE", "Выберите публичный финансовый термин; произвольный текст чата не передаётся в облако.", current=False)
            public_question = "Что такое " + topic + "?"
            if self.knowledge is None:
                return self.answer("unavailable", "PUBLIC_KNOWLEDGE", "Нет разрешённого локального источника знаний. Общий ответ не подтверждён документами.", current=False)
            local = self.knowledge.evidence_answer(public_question, approved_sources=self.public_sources, now=now)
            if local["status"] != "evidence_only":
                return self.answer(local["status"], "PUBLIC_KNOWLEDGE", "Подходящий актуальный источник не доступен. Не буду придумывать ответ.", current=False)
            evidence = local["evidence"]
            if cloud:
                if self.budget is None or self.prices is None:
                    return self.answer("blocked", "PUBLIC_KNOWLEDGE", "Облачный запрос не разрешён: нужен настроенный журнал, каталог и отдельная проверка ключа владельцем.", current=False)
                remote = await self.knowledge.explain(public_question, approved_sources=self.public_sources,
                    budget_path=self.budget, pricing_path=self.prices, transport=self.transport, now=now, reasoning_effort="none", answer_mode="explanation" if explain else "excerpts")
                llm = remote["llm"]
                good = llm.get("status") == "evidence_only" and bool(llm.get("answer"))
                return self.answer("answer_unverified" if good and explain else "evidence_only" if good else "unavailable" if remote["status"] == "unavailable" else "partial", "PUBLIC_KNOWLEDGE",
                    "\n".join(i.get("explanation", i["quote"]) for i in llm["answer"]) if good else "Провайдер или источник не дал актуальный проверенный ответ. Автоповтора нет.",
                    citations=[i["citation"] for i in llm["answer"]] if good else [], cost_rub=llm.get("cost_rub", llm.get("paid_cost_rub")),
                    cached=llm.get("cached", False), model=llm.get("model"), simulation=llm.get("simulation", False),
                    data=remote, generated=good and explain, limitations=["Цитаты проверяются, но истинность перефразирования не доказана" if explain else "Это дословные цитаты, не проверенная свободная генерация"], current=good)
            return self.answer("evidence_only", "PUBLIC_KNOWLEDGE", "Из разрешённых источников:\n" + "\n".join(i["quote"] for i in evidence), citations=[i["citation"] for i in evidence], limitations=local["limitations"] + ["Семантическая релевантность не доказана; цитата не является личным финансовым фактом"], cloud_requested=cloud, cloud_status="not_attempted_local_first")
        return self.answer("insufficient_data", "UNAVAILABLE", "Уточните вопрос: расходы за месяц, очередь проверки, память, источники или публичный финансовый термин. Личные факты не отправляются модели.", current=False)
