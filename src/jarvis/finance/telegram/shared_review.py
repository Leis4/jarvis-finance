"""Owner-bound Telegram views over the existing private review and label journal."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from jarvis.finance.classification.suggestions import LocalSuggestions
from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_store import ReviewStore, encoded
from jarvis.finance.scheduler.telegram_notify import claim_delivery, finish_delivery

FIELDS = frozenset({'merchant', 'description', 'amount', 'date', 'card'})
logger = logging.getLogger(__name__)
NAMES = {'food': 'Продукты', 'food_basic': 'Продукты', 'transport': 'Транспорт',
         'salary': 'Зарплата', 'stipend': 'Стипендия', 'health': 'Здоровье',
         'shopping': 'Покупки', 'subscriptions': 'Подписки', 'own_transfer': 'Свои переводы',
         'refund': 'Возврат', 'entertainment': 'Развлечения'}


class ReviewRefusal(ValueError):
    """Expose only a fixed public diagnostic."""


class Transport(Protocol):
    synthetic: bool
    identity: str

    def call(self, method: str, payload: dict[str, Any]) -> Any: ...


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class SharedReview:
    """Transport metadata is not a second financial queue or label store."""

    def __init__(self, store: ReviewStore, *, now: Callable[[], datetime] | None = None, initialize: bool = True):
        self.store, self.labels = store, HumanLabels(store)
        self.now = now or (lambda: datetime.now(UTC))
        if initialize:
            self._initialize()

    def _initialize(self) -> None:
        # Verify role and immutable evidence before any schema or permission mutation.
        with self.store.connect() as conn:
            self.store._audit(conn)
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS tg_state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tg_pair(code_hash TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tg_buttons(id TEXT PRIMARY KEY,payload TEXT NOT NULL,used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tg_updates(channel TEXT NOT NULL,id INTEGER NOT NULL,state TEXT NOT NULL,
                    PRIMARY KEY(channel,id));
            ''')
            conn.commit()

    def _state(self, conn: sqlite3.Connection) -> dict[str, Any]:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='tg_state'").fetchone():
            return {'enabled': False, 'generation': 0, 'fields': []}
        row = conn.execute('SELECT payload FROM tg_state WHERE id=1').fetchone()
        if row is None:
            return {'enabled': False, 'generation': 0, 'fields': []}
        state = dict(json.loads(row[0]))
        event = next((e for e in reversed(self.store._audit(conn)) if e.get('domain') == 'telegram_review'), None)
        if not event or event.get('permission_hash') != digest(state):
            raise ReviewRefusal('PERMISSION_INTEGRITY_FAILURE')
        return state

    def status(self) -> dict[str, Any]:
        with self.store.connect() as conn:
            state = self._state(conn)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            interrupted = conn.execute("SELECT COUNT(*) FROM tg_updates WHERE state='started'").fetchone()[0] if 'tg_updates' in tables else 0
            deliveries = {r[0]: r[1] for r in conn.execute('SELECT status,COUNT(*) FROM notification_deliveries GROUP BY status')} if 'notification_deliveries' in tables else {}
        return {**state, 'enabled': bool(state['enabled'] and state.get('workspace') == str(self.store.workspace)),
                'token_present': (self.store.workspace / 'telegram.dpapi').is_file(),
                'interrupted_updates': interrupted, 'delivery_states': deliveries,
                'production_database_modified': False}

    def _audit(self, conn: sqlite3.Connection, action: str, state: dict[str, Any]) -> None:
        events = self.store._audit(conn)
        self.store._append_event(conn, {'seq': len(events) + 1, 'domain': 'telegram_review',
            'item_id': 'telegram_permission', 'action': action, 'generation': state['generation'],
            'permission_hash': digest(state), 'timestamp': self.now().isoformat()})

    def disable(self) -> dict[str, Any]:
        self._initialize()
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            state = self._state(conn)
            state.update(enabled=False, generation=state['generation'] + 1)
            conn.execute('INSERT OR REPLACE INTO tg_state VALUES (1,?)', (encoded(state),))
            conn.execute('DELETE FROM tg_pair')
            self._audit(conn, 'disable', state)
            conn.commit()
        return self.status()

    def begin_pair(self, user: int, chat: int, *, synthetic: bool) -> str:
        self._initialize()
        if type(user) is not int or user <= 0 or user != chat:
            raise ReviewRefusal('PRIVATE_CHAT_REQUIRED')
        code = secrets.token_hex(4).upper()
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM tg_pair WHERE json_extract(payload,\'$.expires\')<?', (self.now().isoformat(),))
            if conn.execute('SELECT COUNT(*) FROM tg_pair').fetchone()[0] >= 20:
                raise ReviewRefusal('PAIRING_RATE_LIMIT')
            conn.execute('INSERT INTO tg_pair VALUES (?,?)', (digest(code), encoded({
                'user': user, 'chat': chat, 'synthetic': synthetic, 'generation': self._state(conn)['generation'],
                'expires': (self.now() + timedelta(minutes=5)).isoformat()})))
            conn.commit()
        return code

    def finish_pair(self, code: str, fields: list[str], *, expected_preview: str, finance_reports: bool = False) -> dict[str, Any]:
        self._initialize()
        if not fields or not set(fields) <= FIELDS or len(fields) != len(set(fields)):
            raise ReviewRefusal('EXPLICIT_PRIVACY_FIELDS_REQUIRED')
        if finance_reports and not {'amount', 'date'} <= set(fields):
            raise ReviewRefusal('REPORT_AMOUNT_DATE_PERMISSION_REQUIRED')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if expected_preview != digest(self.preview(fields, finance_reports=finance_reports)):
                raise ReviewRefusal('STALE_PREVIEW')
            row = conn.execute('SELECT payload FROM tg_pair WHERE code_hash=?', (digest(code.strip().upper()),)).fetchone()
            old = self._state(conn)
            if row is None:
                raise ReviewRefusal('INVALID_PAIRING')
            pair = json.loads(row[0])
            if pair['expires'] <= self.now().isoformat() or pair['generation'] != old['generation']:
                raise ReviewRefusal('EXPIRED_PAIRING')
            state = {**pair, 'enabled': True, 'generation': old['generation'] + 1,
                     'fields': fields, 'workspace': str(self.store.workspace), 'finance_reports': finance_reports}
            conn.execute('INSERT OR REPLACE INTO tg_state VALUES (1,?)', (encoded(state),))
            conn.execute('DELETE FROM tg_pair')
            self._audit(conn, 'pair', state)
            conn.commit()
        return self.status()

    @staticmethod
    def _fields(item: dict[str, Any], fields: list[str]) -> dict[str, str]:
        row = item['operation']
        result = {}
        for field in fields:
            if field == 'amount':
                value = str(row.get('amount', 'неизвестно')) + ' ' + str(row.get('currency', 'неизвестно'))
            elif field == 'date':
                value = str(row.get('tx_date') or 'неизвестно')
            elif field == 'card':
                raw = str(item.get('card_last4') or item.get('card') or '')
                value = '••' + raw[-4:] if re.fullmatch(r'[0-9]{4,19}', raw) else 'неизвестно'
            else:
                value = str(row.get(field) or 'неизвестно')
            result[field] = ''.join(c for c in value if c.isprintable())[:500]
        return result

    def preview(self, fields: list[str], *, finance_reports: bool = False) -> dict[str, Any]:
        if not set(fields) <= FIELDS:
            raise ReviewRefusal('INVALID_PRIVACY_FIELDS')
        return {'fields': fields, 'operations': [{'id': i['id'], 'visible': self._fields(i, fields)}
                for i in self.store.items()], 'packet_hash': self.store.metadata()['packet_hash'],
                'finance_reports': finance_reports,
                'report_fields': ['period', 'currency', 'movement', 'income', 'expenses', 'category_totals', 'coverage', 'unresolved', 'source_hash_prefix'] if finance_reports else [],
                'notice': 'Telegram — внешняя облачная платформа. Только выбранные поля и категории.'}

    def financial_report(self, command: str, state: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """Use the same accepted local snapshot; permission never accepts a source."""
        if not state.get('finance_reports') or not {'amount', 'date'} <= set(state['fields']):
            return {'text': 'Финансовые отчёты Telegram отдельно не разрешены. Включите разрешение и preview в локальных настройках.'}, None
        if command == '/balance':
            return {'text': 'Текущие свободные средства неизвестны. Остатки PDF исторические; нужны свежие принятые остатки и обязательства. /report YYYY-MM — проверка исторического периода.'}, None
        parts = command.split()
        if len(parts) == 3 and parts[0] == '/trend':
            try:
                from jarvis.finance.financial_charts import monthly_chart
                from jarvis.finance.statement_product import StatementProduct
                start, stop = (date.fromisoformat(p + '-01') for p in parts[1:])
                reports: list[dict[str, Any]] = []
                while start <= stop and len(reports) < 12:
                    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                    reports.append(StatementProduct(self.store).report(start, end))
                    start = end + timedelta(days=1)
                if start <= stop:
                    raise ValueError('Bounded trend required')
                chart = monthly_chart(reports)
                return {k: chart[k] for k in ('photo_base64', 'caption')}, str(reports[0]['version'])
            except (ValueError, KeyError, OSError):
                return {'text': 'Динамика недоступна: нужны сопоставимые полные месяцы, счета, валюта и подтверждённые категории.'}, None
        if len(parts) != 2 or not re.fullmatch(r'\d{4}-\d{2}', parts[1]):
            return {'text': 'Укажите период: /report YYYY-MM или /cashflow YYYY-MM. Неполные данные не заменяются нулями.'}, None
        try:
            from jarvis.finance.statement_product import StatementProduct
            report = StatementProduct(self.store).month_report(parts[1])
            if parts[0] == '/expenses' and not report['known_by_category']:
                return {'text': 'Подтверждённых расходов для диаграммы пока нет. /review — проверить категории; /chart ' + parts[1] + ' — банковское движение, включая переводы.'}, str(report['version'])
            if parts[0] in {'/chart', '/expenses'}:
                from jarvis.finance.financial_charts import report_chart
                chart = report_chart(report, categories=parts[0] == '/expenses')
                return {**{k: chart[k] for k in ('photo_base64', 'caption')},
                    'reply_markup': {'inline_keyboard': [[{'text': 'Исправить категорию', 'callback_data': 'nav:review'},
                        {'text': 'Спросить Jarvis', 'callback_data': 'nav:ask'}]]}}, str(report['version'])
        except (ValueError, KeyError, OSError):
            return {'text': 'Отчёт недоступен: нет принятого источника или его версия изменилась. Проверьте оригиналы и принятие на сайте. Суммы неизвестны.'}, None
        flow = report['bank_movements']
        lines = ['Финансы по принятым выпискам · ' + ' — '.join(report['period']) + ' RUB',
            'Поступления банка: ' + (flow['credits'] if flow else 'неизвестно'),
            'Списания банка: ' + (flow['debits'] if flow else 'неизвестно'),
            'Движение включает переводы; это не доходы/расходы.',
            'Доходы: ' + (report['income'] or 'неизвестно'),
            'Экономические расходы: ' + (report['expenses'] or 'неизвестно'),
            f"Покрыто счетов: {report['covered_accounts']}/{report['known_accounts']}; непроверенных категорий: {report['unresolved_categories']}",
            'Свободные средства: неизвестно',
            'Источник snapshot: ' + report['source_sha256'][:12],
            'Только принятые выписки; полнота всех финансов неизвестна.']
        # Bounded text chart uses already-computed Decimal totals; no image provider.
        categories = report['by_category']
        if categories:
            from decimal import Decimal
            maximum = max(Decimal(v) for v in categories.values())
            lines.append('Подтверждённые категории (диаграмма):')
            for name, amount in sorted(categories.items(), key=lambda pair: Decimal(pair[1]), reverse=True)[:12]:
                bars = int(Decimal(amount) / maximum * 10) if maximum > 0 else 0
                lines.append(name[:50] + ' ' + '▰' * bars + ' ' + amount)
        return {'text': '\n'.join(lines)}, str(report['version'])

    def _owner(self, conn: sqlite3.Connection, user: int, chat: int, synthetic: bool) -> dict[str, Any]:
        state = self._state(conn)
        if (not state['enabled'] or state.get('workspace') != str(self.store.workspace)
                or state.get('synthetic') is not synthetic or user != state.get('user') or chat != state.get('chat')
                or user != chat or not state['fields']):
            raise ReviewRefusal('OWNER_PERMISSION_REQUIRED')
        return state

    def _binding(self, ident: str) -> str:
        # Full shared audit/packet/parser/policy revisions invalidate stale cards.
        meta = self.store.metadata()
        if 'sources' in meta:
            from jarvis.finance.review_sources import source_pdf
            item = next(i for i in self.store.items() if i['id'] == ident)
            source_pdf(self.store, item['source_sha256'])
        return digest([meta, self.store.audit(), self.labels.audit()])

    def _button(self, text: str, item: dict[str, Any], state: dict[str, Any],
                action: str, category: str | None = None) -> dict[str, str]:
        latest = self.labels.latest().get(item['id'])
        token = secrets.token_urlsafe(18)
        value = {'item': item['id'], 'revision': latest['seq'] if latest else 0, 'binding': self._binding(item['id']),
                 'action': action, 'category': category, 'generation': state['generation'],
                 'expires': (self.now() + timedelta(minutes=10)).isoformat()}
        with self.store.connect() as conn:
            conn.execute('INSERT INTO tg_buttons(id,payload) VALUES (?,?)', (token, encoded(value)))
            conn.commit()
        return {'text': text, 'callback_data': token}

    def card(self, state: dict[str, Any], ident: str | None = None, *, chooser: bool = False) -> dict[str, Any]:
        before = self._binding('' if ident is None else ident) if 'sources' not in self.store.metadata() else None
        items = [i for i in self.store.items() if i['status'] != 'reject']
        # Largest unresolved movement first within currency; amount never proves category.
        from decimal import Decimal, InvalidOperation
        def merchant_key(item: dict[str, Any]) -> str:
            return ' '.join(str(item['operation'].get('merchant') or '').casefold().split())
        repetitions = Counter(merchant_key(i) for i in items if merchant_key(i))
        def priority(item: dict[str, Any]) -> tuple[str, Decimal, str]:
            operation = item['operation']
            try:
                amount = Decimal(str(operation.get('amount', '0')))
                weight = abs(amount) if amount.is_finite() else Decimal(0)
            except InvalidOperation:
                weight = Decimal(0)
            weight *= min(5, repetitions.get(merchant_key(item), 1))
            return str(operation.get('currency', '')), -weight, str(item['id'])
        items.sort(key=priority)
        latest = self.labels.latest()
        item = next((i for i in items if i['id'] == ident), None) if ident else next(
            (i for i in items if latest.get(i['id'], {}).get('action') != 'confirm'), None)
        if item is None:
            result: dict[str, Any] = {'text': 'Очередь пуста. Все доступные категории проверены.'}
            if items:
                result['reply_markup'] = {'inline_keyboard': [[self._button('История категорий', items[0], state, 'details')]]}
            return result
        if before is None:
            # Source originals are verified before and after constructing a card.
            before = self._binding(item['id'])
            latest = self.labels.latest()
        policy = self.labels.policy()
        if not policy:
            return {'text': 'Сначала настройте политику категорий на сайте.'}
        suggestion = LocalSuggestions(self.store).preview(item['id'])
        label = latest.get(item['id'])
        current = label['category'] if label and label['action'] == 'confirm' else None
        category = current or suggestion.suggested_category
        external_proposal = False
        if category is None and any(k.startswith('annotation_result:') for k in self.store.metadata()):
            from jarvis.finance.classification.annotation import AnnotationExchange
            try:
                proposed = AnnotationExchange(self.store).proposals(item_id=item['id'])['proposals']
                if proposed and proposed[0]['proposed_category'] in policy['categories']:
                    category = proposed[0]['proposed_category']
                    external_proposal = True
            except (ValueError, KeyError, OSError):
                pass  # Unavailable/revoked proposal is never substituted with a category.
        text = 'Операция на проверке\n' + '\n'.join(f'{k}: {v}' for k, v in self._fields(item, state['fields']).items())
        text += '\nПриоритет: сумма и повторы внутри валюты; это не докниколайельство расхода.'
        if 'merchant' in state['fields'] and repetitions.get(merchant_key(item), 0) > 1:
            text += f'\nТакой merchant встречается: {repetitions[merchant_key(item)]}. Разметка применяется только к этой операции.'
        text += '\nКатегория: ' + (NAMES.get(current, current) if current else 'не подтверждена')
        text += '\nПредложение Jarvis: ' + (NAMES.get(category, category) if category else 'нет достаточных оснований')
        text += '\nОснование: ' + ('подтверждённые похожие операции' if suggestion.feedback_count else 'локальные правила или отказ от предложения')
        if external_proposal:
            text += '\nAI_PROPOSAL из вручную загруженного файла; ссылки не проверены. Это не human label.'
        if item.get('conflict_reasons'):
            text += '\nКонфликт источника: подтверждение запрещено; проверьте на сайте.'
        buttons = []
        if chooser:
            if item.get('conflict_reasons'):
                text += '\nКатегорию нельзя подтвердить до ручного разбора источника.'
            else:
                buttons = [[self._button(NAMES.get(c, c), item, state, 'confirm', c)] for c in policy['categories']]
        else:
            if category in policy['categories'] and not item.get('conflict_reasons'):
                buttons.append([self._button('Подтвердить категорию', item, state, 'confirm', category)])
            buttons.append([self._button('Изменить', item, state, 'choose'), self._button('Подробнее', item, state, 'details')])
            if current:
                buttons.append([self._button('Отозвать категорию', item, state, 'revoke')])
        pos = items.index(item)
        buttons.append([self._button('Назад', item, state, 'back'), self._button('Пропустить / далее', item, state, 'next')])
        if self._binding(item['id']) != before:
            raise ReviewRefusal('STALE_ACTION')
        return {'text': text + f'\n{pos + 1} из {len(items)}. Это категория, не принятие денег.',
                'reply_markup': {'inline_keyboard': buttons}}

    def callback(self, token: str, user: int, chat: int, synthetic: bool) -> dict[str, Any]:
        def guard(conn: sqlite3.Connection) -> None:
            state = self._owner(conn, user, chat, synthetic)
            row = conn.execute('SELECT payload,used FROM tg_buttons WHERE id=?', (token,)).fetchone()
            if not row or row[1]:
                raise ReviewRefusal('STALE_ACTION')
            value = json.loads(row[0])
            if (value['generation'] != state['generation'] or value['expires'] <= self.now().isoformat()
                    or value['binding'] != self._binding(value['item'])):
                raise ReviewRefusal('STALE_ACTION')
            conn.execute('UPDATE tg_buttons SET used=1 WHERE id=?', (token,))
        with self.store.connect() as conn:
            row = conn.execute('SELECT payload FROM tg_buttons WHERE id=?', (token,)).fetchone()
        if not row:
            raise ReviewRefusal('STALE_ACTION')
        value = json.loads(row[0])
        if value['action'] in {'confirm', 'revoke'}:
            try:
                event = self.labels.decide(value['item'], value['action'], actor=f'telegram:{user}:{chat}',
                    reason='Explicit Telegram category decision', expected_revision=value['revision'],
                    category=value['category'], transaction_guard=guard)
            except ReviewRefusal:
                raise
            except ValueError:
                raise ReviewRefusal('STALE_ACTION') from None
            message = 'Категория сохранена: ' + NAMES.get(event['category'], str(event['category'])) if event['action'] == 'confirm' else 'Категория отозвана.'
            return {'text': message, 'reply_markup': self.card(self.status(), value['item']).get('reply_markup', {})}
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            guard(conn)
            conn.commit()
        ident = value['item']
        if value['action'] in {'next', 'back'}:
            ids = [i['id'] for i in self.store.items() if i['status'] != 'reject']
            ident = ids[(ids.index(ident) + (1 if value['action'] == 'next' else -1)) % len(ids)]
        if value['action'] not in {'next', 'back', 'choose', 'details'}:
            raise ReviewRefusal('INVALID_ACTION')
        result = self.card(self.status(), ident, chooser=value['action'] == 'choose')
        if value['action'] == 'details':
            result['text'] += '\nИсточник и история доступны на локальном сайте. Сырые PDF и скрытые поля не пересылаются.'
            history = [e for e in self.labels.audit() if e['item_id'] == ident][-5:]
            result['text'] += '\nИстория категории: ' + (' → '.join(
                NAMES.get(e['category'], str(e['category'])) if e['action'] == 'confirm' else 'отозвана / отклонена'
                for e in history) or 'решений пока нет')
        return result

    def handle(self, update: dict[str, Any], transport: Transport) -> str:
        """Claim update before effects; ambiguous outcomes are never automatically resent."""
        uid = update.get('update_id')
        if type(uid) is not int or uid < 0:
            raise ReviewRefusal('INVALID_UPDATE')
        cb = update.get('callback_query')
        message = (cb.get('message', {}) if cb else update.get('message', {}))
        chat = message.get('chat', {})
        user = (cb or message).get('from', {}).get('id')
        cid = chat.get('id')
        channel = transport.identity
        outcome = 'done'
        state: dict[str, Any] | None = None
        financial_version: str | None = None
        result: dict[str, Any]
        try:
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute('SELECT 1 FROM tg_updates WHERE channel=? AND id=?', (channel, uid)).fetchone():
                    return 'duplicate'
                conn.execute("INSERT INTO tg_updates VALUES (?,?,'started')", (channel, uid))
                conn.commit()
            if chat.get('type') != 'private' or type(user) is not int or user != cid:
                raise ReviewRefusal('PRIVATE_CHAT_REQUIRED')
            text = str(message.get('text', ''))
            if not cb and text == '/start' and not self.status()['enabled']:
                result = {'text': 'Код привязки на 5 минут: ' + self.begin_pair(user, cid, synthetic=transport.synthetic) + '\nВведите его только в локальных настройках Jarvis.'}
            elif not cb and text == '/review' and not self.status()['enabled']:
                result = {'text': 'Финансовые поля не разрешены. Отправьте /start и завершите привязку с preview в локальных настройках Jarvis.'}
            else:
                with self.store.connect() as conn:
                    state = self._owner(conn, user, cid, transport.synthetic)
                if cb and str(cb.get('data', '')).startswith('nav:'):
                    command = str(cb['data'])[4:]
                    if command == 'review':
                        result = self.card(state)
                    elif command == 'ask':
                        result = {'text': 'Напишите /ask Сколько я потратил на продукты с YYYY-MM-DD по YYYY-MM-DD?\nОтдельно включённый Qwen: /analyst YYYY-MM. Без такого разрешения облачного вызова нет.'}
                    elif re.fullmatch(r'/(?:report|cashflow|expenses|chart) \d{4}-\d{2}', command):
                        result, financial_version = self.financial_report(command, state)
                    else:
                        raise ReviewRefusal('INVALID_NAVIGATION')
                elif cb:
                    result = self.callback(str(cb.get('data', '')), user, cid, transport.synthetic)
                elif text == '/review':
                    result = self.card(state)
                elif text == '/status':
                    progress = self.labels.progress()
                    result = {'text': f"Ожидают категории: {progress['pending_categories']}; подтверждены: {progress['human_confirmed']}"}
                elif text in {'/start', 'Мои финансы', 'Расходы', '/report', '/cashflow', '/expenses'}:
                    from jarvis.finance.statement_product import StatementProduct
                    sources = StatementProduct(self.store).sources()
                    months = sorted({s['metadata']['period'][1][:7] for s in sources if s['accepted']}, reverse=True)[:6]
                    result = {'text': 'Мои финансы · выберите период. Расходы включают только подтверждённые категории.\n/ask вопрос · /review исправление · /balance остатки',
                        'reply_markup': {'inline_keyboard': [[{'text': 'Расходы ' + month, 'callback_data': 'nav:/expenses ' + month},
                            {'text': 'Движение ' + month, 'callback_data': 'nav:/chart ' + month}] for month in months]}}
                elif text == '/budget':
                    result = {'text': 'Бюджет неизвестен: нужны подтверждённые доходы, обязательства, резерв и свежие остатки. Исторические поступления не равны доходам. Задайте сценарий с явными параметрами в финансовой панели.'}
                elif text.startswith('/analyst '):
                    if not state.get('finance_reports') or not {'amount', 'date'} <= set(state['fields']):
                        raise ReviewRefusal('FINANCIAL_REPORT_PERMISSION_REQUIRED')
                    from jarvis.finance.financial_analyst import analyze_month
                    from jarvis.finance.statement_product import StatementProduct
                    financial_version = StatementProduct(self.store)._binding(self.store)
                    analysis_result = asyncio.run(analyze_month(self.store, text.split(' ', 1)[1]))
                    result = {'text': analysis_result.get('text') or 'Анализ недоступен; журнал расходов сохранён, автоматического повтора нет.'}
                elif text.split(' ', 1)[0] in {'/report', '/cashflow', '/balance', '/expenses', '/chart', '/trend'}:
                    result, financial_version = self.financial_report(text, state)
                elif text.startswith('/ask '):
                    if not state.get('finance_reports') or not {'amount', 'date'} <= set(state['fields']):
                        result = {'text': 'Финансовый /ask требует отдельного preview/разрешения отчётов и полей amount/date на сайте.'}
                    else:
                        from jarvis.services.assistant import AssistantService
                        answer = asyncio.run(AssistantService(self.store).ask(text[5:],
                            conversation_id=digest([channel, user, state['generation'], 'finance-chat'])[:32],
                            request_id=digest([channel, uid, 'finance-chat'])[:32], statement_only=True))
                        result = {'text': answer['text'] or 'Финансовый ответ недоступен.'}
                        financial_version = answer.get('data', {}).get('snapshot_version')
                elif text == '/stop':
                    self.disable()
                    result = {'text': 'Telegram отключён. Решения сохранены.'}
                else:
                    result = {'text': 'Личная проверка: /review, /status. Отдельно разрешённые финансы: /report YYYY-MM, /cashflow YYYY-MM, /balance. /stop — отключить. Денежные действия недоступны.'}
            # Same durable notification journal as scheduler, on private review DB only.
            key = f'tg{channel}_{cid}:{uid}'
            if claim_delivery(self.store, key, encoded(result)) == 'claimed':
                try:
                    with self.store.connect() as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        if state is not None and text != '/stop':
                            current = self._owner(conn, user, cid, transport.synthetic)
                            if current['generation'] != state['generation']:
                                raise ReviewRefusal('OWNER_PERMISSION_REQUIRED')
                        if financial_version is not None:
                            from jarvis.finance.statement_product import (
                                StatementProduct,
                            )
                            if StatementProduct(self.store)._binding(self.store) != financial_version:
                                raise ReviewRefusal('STALE_FINANCIAL_REPORT')
                        transport.call('sendPhoto' if 'photo_base64' in result else 'sendMessage', {'chat_id': cid, **result})
                        conn.commit()
                except Exception:  # noqa: BLE001 - fixed transport diagnostic, never log token-bearing exceptions
                    finish_delivery(self.store, key, False)
                    raise ReviewRefusal('DELIVERY_UNKNOWN') from None
                finish_delivery(self.store, key, True)
            self._finish(uid, 'done', channel)
            return 'done'
        except (ValueError, KeyError, TypeError) as exc:
            outcome = str(exc) if isinstance(exc, ReviewRefusal) else 'REVIEW_UNAVAILABLE'
            self._finish(uid, 'refused', channel)
            return 'refused'
        finally:
            if cb:
                try:
                    transport.call('answerCallbackQuery', {'callback_query_id': str(cb.get('id', '')),
                        'text': 'STALE_ACTION: обновите /review' if outcome == 'STALE_ACTION' else
                        ('Действие принято' if outcome == 'done' else 'Действие недоступно. Проверьте настройки на сайте.')})
                except Exception:  # noqa: BLE001 - no token-bearing exception text or automatic resend
                    logger.warning('Telegram callback acknowledgment unavailable; no automatic resend')

    def _finish(self, uid: int, state: str, channel: str) -> None:
        with self.store.connect() as conn:
            conn.execute('UPDATE tg_updates SET state=? WHERE channel=? AND id=?', (state, channel, uid))
            conn.commit()
