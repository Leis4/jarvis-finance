"""Owner-only CFO adapter reusing the existing Telegram API and financial core."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from jarvis.finance.personal_cfo import PersonalCFO, money
from jarvis.finance.personal_product import RuntimeSettings, undo_correction
from jarvis.finance.personal_profile import GoalInput, business_date
from jarvis.finance.personal_telegram_menu import main_menu
from jarvis.finance.personal_ui import question
from jarvis.finance.telegram.bot import TelegramAPI, TelegramConfig

MENU = {'keyboard': [[{'text': '💳 Деньги'}, {'text': '📊 Расходы'}],
    [{'text': '🎯 Цели'}, {'text': '🧾 Проверить операции'}],
    [{'text': '🛒 Покупка'}, {'text': '🛡 Статус'}], [{'text': '🧠 Советник'}, {'text': '⚙ Сводки'}]], 'resize_keyboard': True}


class PersonalTelegram:
    """Only the configured private owner may inspect or confirm CFO decisions."""

    def __init__(self, cfo: PersonalCFO, config: TelegramConfig, api: TelegramAPI | None = None):
        self.cfo, self.config = cfo, config
        self.api = api or (TelegramAPI(config.token) if config.token else None)
        self.owner = next(iter(config.allowed_chat_ids)) if len(config.allowed_chat_ids) == 1 else 0
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.verified = False
        self.started_at = int(datetime.now(UTC).timestamp())

    def status(self, status: str, **safe: Any) -> None:
        previous = self.cfo.db.get_setting('cfo.telegram_state', {})
        self.cfo.db.set_setting('cfo.telegram_state', {**previous, 'status': status, 'checked_at': datetime.now(UTC).isoformat(), **safe})

    def verify(self) -> bool:
        self.verified = False
        if not self.api or self.owner <= 0:
            self.status('not_configured')
            return False
        try:
            me = self.api.request('getMe', {})
            chat = self.api.request('getChat', {'chat_id': self.owner})
            hook = self.api.request('getWebhookInfo', {})
            if (not me.get('ok') or not chat.get('ok') or not hook.get('ok')
                    or chat['result'].get('type') != 'private' or chat['result'].get('id') != self.owner):
                self.status('owner_private_chat_unverified')
                return False
            if hook['result'].get('url'):
                self.status('existing_webhook_active')
                return False
            username = str(me['result'].get('username', ''))
            self.verified = True
            self.status('ready', bot_username=username if username.replace('_', '').isalnum() else '')
            return True
        except Exception as exc:  # noqa: BLE001 - token-bearing HTTP errors must not escape
            self.status('unavailable', error_type=type(exc).__name__)
            return False

    def claim(self, key: str) -> bool:
        with self.cfo.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            result = conn.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',
                (key, json.dumps({'status': 'attempted', 'at': datetime.now(UTC).isoformat()})))
            return result.rowcount == 1

    def summary(self) -> str:
        from jarvis.finance.personal_briefing import daily_briefing
        return str(daily_briefing(self.cfo)['text'])

    def weekly_summary(self) -> str:
        from jarvis.finance.personal_briefing import daily_briefing
        return str(daily_briefing(self.cfo, weekly=True)['text'])

    def deliver_summary(self, event: str) -> dict[str, Any]:
        return self.deliver(event, self.summary(), MENU)

    def deliver(self, event: str, text: str, markup: dict[str, Any] | None = None) -> dict[str, Any]:
        """Claim before sending; uncertain delivery is never retried automatically."""
        if not self.api or not self.verified:
            return {'status': 'unavailable'}
        key = 'cfo.telegram_delivery:' + hashlib.sha256((str(self.owner)+event).encode()).hexdigest()
        if not self.claim(key):
            return {'status': 'not_repeated'}
        try:
            payload: dict[str, Any] = {'chat_id': self.owner, 'text': text}
            if markup:
                payload['reply_markup'] = json.dumps(markup)
            result = self.api.request('sendMessage', payload)
            if not result.get('ok'):
                receipt = {'status': 'failed', 'at': datetime.now(UTC).isoformat()}
            else:
                receipt = {'status': 'delivered', 'message_id': result['result']['message_id'], 'at': datetime.now(UTC).isoformat()}
            self.cfo.db.set_setting(key, receipt)
            return receipt
        except Exception as exc:  # noqa: BLE001 - ambiguous sends are not safe to retry
            receipt = {'status': 'unknown', 'error_type': type(exc).__name__}
            self.cfo.db.set_setting(key, receipt)
            return receipt

    def review(self, tx_id: int | None = None) -> dict[str, Any]:
        """Inspect the complete local queue, with version-bound owner actions."""
        rows = self.cfo.db.all_transactions()
        deferred = self.cfo.db.get_setting('cfo.telegram_deferred', {})
        now = datetime.now(UTC).isoformat()
        row = next((r for r in rows if r['id'] == tx_id), None) if tx_id else next((r for r in rows
            if r['category'] in {'unknown', 'pending_review', 'uncategorized'}
            and deferred.get(str(r['id']), '') <= now), None)
        if row is None:
            return {'text': 'Нет доступных операций для проверки. Отложенные вернутся через сутки.', 'reply_markup': MENU}
        with self.cfo.db.connect() as conn:
            latest = conn.execute('SELECT * FROM category_corrections WHERE transaction_id=? ORDER BY version DESC LIMIT 1', (row['id'],)).fetchone()
        version = latest['version'] if latest else 0
        categories = [('Продукты','food_basic'),('Транспорт','transport'),('Здоровье','health'),('Подписка','subscriptions'),('Маркетплейс','shopping_marketplace')]
        buttons = [[{'text': name, 'callback_data': f"cf:{row['id']}:{cat}:{version}"}] for name,cat in categories]
        if latest and latest['source'] == 'manual':
            buttons.append([{'text': 'Отменить подтверждение', 'callback_data': f"cu:{row['id']}:{version}"}])
        buttons.append([{'text': 'Отложить на сутки', 'callback_data': f"cd:{row['id']}:{version}"}])
        origin = f"Подтверждено владельцем, версия {version}" if latest and latest['source'] == 'manual' else 'Не подтверждено владельцем'
        proposal = self.cfo.db.get_setting('cfo.ai_proposal:'+str(row['id']), {}).get('result', {})
        hint = f"\nПредложение: {proposal.get('category', 'неизвестно')} (не подтверждено)." if proposal else ''
        return {'text': f"Операция #{row['id']} · {row['tx_date']} · {row['amount']} {row['currency']}\n{row['merchant'][:100]}\nКатегория: {row['category']}. {origin}.{hint}\nВыберите только известную вам категорию. /review ID — изменить или отменить подтверждение.",
            'reply_markup': {'inline_keyboard': buttons}}

    def _delete_message(self, message_id: Any) -> None:
        """Best-effort removal of a secret-bearing owner message; never block recovery."""
        if not self.api or message_id is None:
            return
        try:
            self.api.request('deleteMessage', {'chat_id': self.owner, 'message_id': int(message_id)})
        except Exception:  # noqa: BLE001, S110 - deletion is private-chat best effort
            pass

    def _clear_sync_hold(self) -> None:
        jobs = self.cfo.db.get_setting('cfo.jobs', {})
        sync = jobs.get('sync', {})
        if sync.get('status') == 'auth_required':
            now = datetime.now(UTC)
            jobs['sync'] = {**sync, 'status': 'updated', 'code': None, 'errors': 0,
                'owner_action_required': False, 'at': now.isoformat(),
                'next_at': (now+timedelta(minutes=30)).isoformat()}
            self.cfo.db.set_setting('cfo.jobs', jobs)

    def _profile_reply(self, arguments: str) -> str:
        from jarvis.finance.personal_profile import AmountFact, PersonalProfile
        profile = self.cfo.profile()
        updated = False
        for token in arguments.replace('|', ' ').split():
            if '=' not in token:
                return 'Формат: /profile reserve=10000 protected=0 cash=5000 obligations=ok'
            key, value = (part.strip() for part in token.split('=', 1))
            if key in {'cash', 'reserve', 'protected'}:
                try:
                    fact = None if value.lower() in {'none', 'unknown', '?'} else AmountFact(low=value)
                except ValueError:
                    return 'Нужна неотрицательная сумма или none: reserve=10000 protected=0 cash=none.'
                values = profile.model_dump(mode='json')
                values[key] = fact.model_dump(mode='json') if fact else None
                profile = PersonalProfile.model_validate(values)
                updated = True
            elif key in {'obligations', 'obligations_complete'}:
                if value.lower() in {'ok', 'true', '1', 'yes'}:
                    values = profile.model_dump(mode='json'); values['obligations_complete'] = True
                elif value.lower() in {'incomplete', 'false', '0', 'no'}:
                    values = profile.model_dump(mode='json'); values['obligations_complete'] = False
                else:
                    return 'obligations: ok или incomplete.'
                profile = PersonalProfile.model_validate(values)
                updated = True
            else:
                return 'Поля: cash, reserve, protected, obligations=ok|incomplete.'
        if updated:
            self.cfo.save_profile(profile)
        current = self.cfo.profile()
        position = self.cfo.dashboard()['financial_position']
        missing = position.get('missing') or []
        free = position['capacity']['scenarios']['keep_money']['headroom'] if not missing else None
        def show(fact: Any) -> str:
            return fact.low + ('–'+fact.high if fact.high and fact.high != fact.low else '') if fact else 'не задано'
        goals = self.cfo.goals(business_date(datetime.now(UTC)))
        lines = [
            f"Профиль: наличные {show(current.cash)}, резерв {show(current.reserve)}, защищённые {show(current.protected)}.",
            (f"Обязательства: {'полные' if current.obligations_complete else 'неполные'}; "
             f"доходов {len(current.incomes)}, обязательств {len(current.obligations)}, целей {len(goals)}."),
            f"Доступно к трате: {free+' ₽' if free is not None else 'неизвестно'}.",
        ]
        if missing:
            lines.append('Не хватает: '+', '.join(missing)+'.')
        lines.append('Задать: /profile reserve=10000 protected=0 obligations=ok')
        return '\n'.join(lines)

    def _onboarding_reply(self, arguments: str) -> dict[str, Any]:
        from jarvis.finance.classification.learning import onboarding_queue
        try:
            index = int(arguments.strip() or '0')
        except ValueError:
            return {'text': 'Формат: /onboarding [номер группы]', 'reply_markup': MENU}
        cutoff = business_date(datetime.now(UTC))-timedelta(days=14)
        groups = onboarding_queue(self.cfo, cutoff=cutoff)['groups']
        if not groups:
            self.cfo.db.set_setting('cfo.telegram_onboarding', {'active': False})
            return {'text': 'Стартовых групп нет. Сначала подготовьте предложения в веб-панели.', 'reply_markup': MENU}
        rows = {row['id']: row for row in self.cfo.db.all_transactions()}
        chosen = None
        for offset in range(len(groups)):
            candidate = groups[(index+offset) % len(groups)]
            row = rows.get(candidate['representative_id'])
            if row is None:
                continue
            with self.cfo.db.connect() as conn:
                latest = conn.execute('SELECT * FROM category_corrections WHERE transaction_id=? ORDER BY version DESC LIMIT 1',
                    (row['id'],)).fetchone()
            if latest and latest['source'] == 'manual':
                continue
            chosen = (candidate, row, latest)
            break
        if chosen is None:
            self.cfo.db.set_setting('cfo.telegram_onboarding', {'active': False})
            return {'text': 'Все рекомендованные стартовые операции уже подтверждены. /review — очередь.', 'reply_markup': MENU}
        candidate, row, latest = chosen
        self.cfo.db.set_setting('cfo.telegram_onboarding', {'active': True, 'index': index})
        version = latest['version'] if latest else 0
        categories = [('Продукты','food_basic'),('Транспорт','transport'),('Здоровье','health'),
            ('Подписка','subscriptions'),('Маркетплейс','shopping_marketplace'),('Поддержка семьи','family_income')]
        buttons = [[{'text': name, 'callback_data': f"cf:{row['id']}:{cat}:{version}"}] for name, cat in categories]
        buttons.append([{'text': 'Следующая группа', 'callback_data': f'on:{index+1}'}])
        return {'text': f"Старт {index+1}/{len(groups)}: {row['merchant']} · {row['tx_date']} · {row['amount']} {row['currency']}.\n"
            f"Причина: {', '.join(candidate['reasons'])}. Проверьте состав покупки; это не автоматическое подтверждение.",
            'reply_markup': {'inline_keyboard': buttons}}

    def _fmt(self, value: Any) -> str:
        try:
            number = Decimal(str(value))
            if number == number.to_integral():
                return f'{number:,.0f}'.replace(',', ' ') + ' ₽'
            return f'{number:,.2f}'.replace(',', ' ') + ' ₽'
        except (ArithmeticError, ValueError, TypeError):
            return '—'

    def _menu_keyboard(self) -> dict[str, Any]:
        try:
            from jarvis.finance.classification.learning import proposal_groups
            pending = proposal_groups(self.cfo)['pending_count']
        except Exception:  # noqa: BLE001 - menu must stay available
            pending = None
        return main_menu(pending)

    def _analysis_reply(self) -> dict[str, Any]:
        return {'text': 'Аналитика · только известные операции. Что посмотреть?',
            'reply_markup': self._nav([
                [{'text': 'Что изменилось?', 'callback_data': 'm:changes'}],
                [{'text': 'Подписки', 'callback_data': 'm:subscriptions'}],
                [{'text': 'Хватит ли до поступления?', 'callback_data': 'm:forecast'}],
            ])}

    def _ai_reply(self) -> dict[str, Any]:
        return {'text': 'Советник опирается на локальные данные. Денежные расчёты выполняются без AI. '
            'Платные пояснения доступны в веб-интерфейсе; общий лимит — 250 ₽ в месяц. '
            'Открытие этого меню не вызывает модель.', 'reply_markup': self._nav([
                [{'text': 'Чему научился Jarvis?', 'callback_data': 'm:learned'}],
                [{'text': 'Можно ли купить?', 'callback_data': 'm:buy'}],
            ])}

    def _settings_reply(self) -> dict[str, Any]:
        settings = RuntimeSettings.model_validate(self.cfo.db.get_setting('cfo.runtime_settings', {}))
        text = (f'Настройки\nЧтение банка: {"включено" if settings.bank_enabled else "выключено"}\n'
            f'Интервал: {settings.sync_minutes} минут\n'
            f'Дневная сводка: {"включена" if settings.daily_enabled else "выключена"}\n'
            f'Недельная сводка: {"включена" if settings.weekly_enabled else "выключена"}\n'
            'Изменить настройки можно в веб-интерфейсе. Здесь показано сохранённое расписание; '
            'работа фонового процесса этим не подтверждается.')
        return {'text': text, 'reply_markup': self._nav()}

    def _nav(self, extra: list[list[dict[str, str]]] | None = None) -> dict[str, Any]:
        rows = list(extra or [])
        rows.append([{'text': '🏠 Меню', 'callback_data': 'm:menu'}])
        return {'inline_keyboard': rows}

    def _menu_reply(self) -> dict[str, Any]:
        from jarvis.finance.personal_briefing import daily_briefing
        return {'text': daily_briefing(self.cfo)['text'] + '\n\nВыберите раздел в меню ниже.',
            'reply_markup': self._menu_keyboard()}

    def _balance_reply(self) -> dict[str, Any]:
        data = self.cfo.dashboard()
        position = data['financial_position']
        free = position.get('headroom')
        if position.get('missing') or position.get('blocking_missing') or not data.get('bank_fresh'):
            free = None
        lines = ['💳 Счета и баланс', '', f'Банк: {self._fmt(data.get("bank_rub"))}']
        for account in (data.get('bank') or {}).get('accounts', []):
            if account.get('currency') != 'RUB':
                continue
            label = str(account.get('name') or 'Счёт')[-4:]
            lines.append(f'• {label}: {self._fmt(account.get("amount"))}')
        lines += ['', f'Свободно к трате: {self._fmt(free) if free is not None else "неизвестно"}',
            f'Дневной ориентир: {self._fmt(data.get("daily_spend_baseline"))}']
        if (data.get('last_refresh') or {}).get('status') == 'auth_required':
            lines += ['', '🔒 Сбер ждёт PIN-код — отправьте /pin 12345']
        elif not data.get('bank_fresh'):
            lines += ['', 'Банковский баланс не сегодняшний — нажмите «Обновить Сбер».']
        return {'text': '\n'.join(lines), 'reply_markup': self._nav()}

    def _spend_reply(self) -> dict[str, Any]:
        from jarvis.finance.event_taxonomy import category_title
        from jarvis.finance.personal_queries import analytics
        data = analytics(self.cfo)
        month = data['periods']['month']
        previous = data['periods']['previous_month']
        items = sorted(((key, money(value)) for key, value in month['by_category'].items()),
            key=lambda pair: pair[1], reverse=True)
        total = sum((value for _, value in items), Decimal(0))
        previous_total = sum((money(value) for value in previous['by_category'].values()), Decimal(0))
        lines = ['📊 Траты месяца', '', f'Всего: {self._fmt(total)}']
        if previous_total:
            delta = (total - previous_total) / previous_total * 100
            lines.append(f'Разность известных сумм: {delta:+.0f}%. Периоды и покрытие различаются; это не доказанный рост.')
        for key, value in items[:6]:
            percent = round(float(value / total * 100)) if total else 0
            filled = max(0, min(5, round(percent / 20)))
            lines.append(f'{category_title(key)}  {"▰"*filled}{"▱"*(5-filled)} {percent}% · {self._fmt(value)}')
        subscriptions = [row for row in self.cfo.dashboard()['patterns'] if row.get('kind') == 'expense_candidate']
        if subscriptions:
            lines += ['', 'Подписки и регулярные:']
            lines += [f'• {row.get("merchant") or "Платёж"}: {self._fmt(row.get("typical_net_expense"))}' for row in subscriptions[:3]]
        if not items:
            lines.append('Пока нет известных расходов за этот месяц.')
        return {'text': '\n'.join(lines),
            'reply_markup': self._nav([[{'text': '🖼 Скачать карточку-инфографику', 'callback_data': 'm:card'}]])}

    def _goals_reply(self, page: int = 0) -> dict[str, Any]:
        goals = self.cfo.goals(business_date(datetime.now(UTC)))
        if not goals:
            return {'text': '🎯 Цели\n\nЦели пока не заданы. Добавьте их в панели или командой /goal.',
                'reply_markup': self._nav()}
        pages = (len(goals) + 4) // 5
        page = max(0, min(page, pages - 1))
        lines = [f'🎯 Мои цели · {page + 1}/{pages}', '']
        for goal in goals[page * 5:(page + 1) * 5]:
            lines.append(f'{str(goal["name"])[:80]}: {self._fmt(goal["current_amount"])} / {self._fmt(goal["target_amount"])}')
            contribution = (goal.get('projection') or {}).get('required_monthly')
            if contribution:
                lines.append(f'  Нужно в месяц: {self._fmt(contribution)}')
        navigation = []
        if page:
            navigation.append({'text': '← Назад', 'callback_data': f'gp:{page - 1}'})
        if page + 1 < pages:
            navigation.append({'text': 'Далее →', 'callback_data': f'gp:{page + 1}'})
        return {'text': '\n'.join(lines), 'reply_markup': self._nav([navigation] if navigation else None)}

    def _operations_reply(self) -> dict[str, Any]:
        from jarvis.finance.classification.learning import proposal_groups
        groups = proposal_groups(self.cfo)['groups']
        skipped = set(self.cfo.db.get_setting('cfo.telegram_skipped_groups', []))
        group = next((row for row in groups if row['id'] not in skipped), None)
        if group is None:
            return {'text': '✨ Разбор операций\n\nВсё разобрано. Спасибо!', 'reply_markup': self._nav()}
        token = hashlib.sha256(group['id'].encode()).hexdigest()[:8]
        items = [{key: item[key] for key in ('id', 'expected_version', 'input_sha256')} for item in group['items']]
        self.cfo.db.set_setting('cfo.telegram_group:'+token, {'group_id': group['id'], 'items': items,
            'created_at': datetime.now(UTC).isoformat()})
        total = sum((abs(money(item['amount'])) for item in group['items']), Decimal(0))
        categories = [('🛒 Продукты', 'food_basic'), ('🍔 Кафе и доставка', 'cafes'),
            ('🚕 Транспорт', 'transport'), ('🛍 Маркетплейсы', 'shopping_marketplace'),
            ('🎮 Подписки и игры', 'subscriptions'), ('💊 Здоровье', 'health'),
            ('💸 Переводы', 'peer_transfer')]
        rows = [[{'text': name, 'callback_data': f'gc:{token}:{category}'}] for name, category in categories]
        rows.append([{'text': '🤖 Авто-разбор с Qwen + Web', 'callback_data': 'm:aienrich'}])
        rows.append([{'text': '⏭ Пропустить', 'callback_data': f'gs:{token}'}])
        return {'text': f'✨ {group["merchant"]}\n\nОпераций: {len(group["items"])} · Сумма: {self._fmt(total)}\n'
            'Выберите категорию — вся группа подтвердится сразу.', 'reply_markup': self._nav(rows)}

    def _confirm_group(self, token: str, category: str | None) -> dict[str, Any]:
        from jarvis.finance.personal_product import review_batch
        key = 'cfo.telegram_group:'+token
        payload = self.cfo.db.get_setting(key, {})
        if not payload or payload.get('used'):
            return self._operations_reply()
        try:
            review_batch(self.cfo, payload['group_id'], payload['items'], category)
        except ValueError:
            return {'text': 'Не удалось подтвердить группу: данные уже изменились. Показываю следующую.',
                'reply_markup': self._nav()}
        self.cfo.db.set_setting(key, {**payload, 'used': True})
        return self._operations_reply()

    def _skip_group(self, token: str) -> dict[str, Any]:
        payload = self.cfo.db.get_setting('cfo.telegram_group:'+token, {})
        skipped = self.cfo.db.get_setting('cfo.telegram_skipped_groups', [])
        if payload.get('group_id') and payload['group_id'] not in skipped:
            skipped.append(payload['group_id'])
            self.cfo.db.set_setting('cfo.telegram_skipped_groups', skipped[-100:])
        return self._operations_reply()

    def _refresh_reply(self) -> dict[str, Any]:
        try:
            result = self.cfo.refresh(allow_pin=False)
        except Exception:  # noqa: BLE001 - no details or secrets in the owner reply
            return {'text': 'Сбер сейчас не ответил. Попробуйте позже.', 'reply_markup': self._nav()}
        if result.get('status') == 'updated':
            return self._balance_reply()
        if result.get('status') == 'auth_required':
            return {'text': '🔒 Сбер ждёт PIN-код — отправьте /pin 12345', 'reply_markup': self._nav()}
        return {'text': 'Обновление не завершено. Попробуйте ещё раз позже.', 'reply_markup': self._nav()}

    def _card_image(self) -> bytes:
        from io import BytesIO

        from PIL import Image, ImageDraw, ImageFont

        from jarvis.finance.event_taxonomy import category_title
        from jarvis.finance.personal_queries import analytics
        data = analytics(self.cfo)
        items = sorted(((key, money(value)) for key, value in data['periods']['month']['by_category'].items()),
            key=lambda pair: pair[1], reverse=True)[:6]
        total = sum((value for _, value in items), Decimal(0))
        image = Image.new('RGB', (900, 1200), '#0F1115')
        draw = ImageDraw.Draw(image)
        bold: Any = ImageFont.load_default()
        text: Any = bold
        small: Any = bold
        big: Any = bold
        try:
            bold = ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf', 52)
            text = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 30)
            small = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 24)
            big = ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf', 78)
        except OSError:
            pass
        draw.text((64, 60), 'JARVIS', font=text, fill='#3B82F6')
        draw.text((64, 120), 'Траты месяца', font=bold, fill='#F9FAFB')
        draw.text((64, 210), self._fmt(total), font=big, fill='#F9FAFB')
        y = 360
        for key, value in items:
            draw.text((64, y), category_title(key), font=text, fill='#D1D5DB')
            draw.text((760, y), self._fmt(value), font=small, fill='#D1D5DB', anchor='ra')
            draw.rounded_rectangle((64, y+48, 836, y+64), radius=8, fill='#181B22')
            width = int(772 * float(value / total)) if total else 0
            draw.rounded_rectangle((64, y+48, 64+width, y+64), radius=8, fill='#3B82F6')
            y += 112
        draw.text((64, 1120), 'Только известные операции · не гарантия полноты', font=small, fill='#6B7280')
        buffer = BytesIO(); image.save(buffer, format='PNG'); return buffer.getvalue()

    def _ai_enrich_reply(self) -> dict[str, Any]:
        from jarvis.finance.classification.intelligence import enrich_unknown_batch
        try:
            result = asyncio.run(enrich_unknown_batch(self.cfo))
        except Exception:  # noqa: BLE001 - optional AI must not break the menu
            return {'text': 'AI-разбор сейчас недоступен. Локальные предложения сохранены.',
                'reply_markup': self._nav()}
        items = result.get('items') or []
        if not items:
            return {'text': '✨ AI-разбор\n\nНераспознанных групп не осталось.', 'reply_markup': self._nav()}
        lines = ['✨ AI-разбор и обучение', '']
        for item in items[:5]:
            if item.get('category'):
                lines.append(f'• {item["merchant"]}: {item["category"]}')
                if item.get('web'):
                    lines.append(f'  Найдено: {item["web"][0]["title"]}')
            else:
                lines.append(f'• {item["merchant"]}: нужно уточнить')
        lines += ['', 'Новые правила сохранены; будущие операции этого магазина разберутся сразу.']
        return {'text': '\n'.join(lines), 'reply_markup': self._nav()}

    def _card_reply(self) -> dict[str, Any]:
        try:
            return {'text': '🖼 Карточка за месяц', 'photo': self._card_image()}
        except Exception:  # noqa: BLE001 - image failure must not break the chat
            return {'text': 'Не удалось собрать карточку. Попробуйте экран «Траты месяца».',
                'reply_markup': self._nav()}

    def handle(self, update: dict[str, Any]) -> dict[str, Any] | None:
        """Return a closed message reply; incoming content never gains tool authority."""
        callback = update.get('callback_query') or {}
        message = callback.get('message') or update.get('message') or {}
        sender = callback.get('from') or message.get('from') or {}
        chat = message.get('chat') or {}
        if sender.get('id') != self.owner or chat.get('id') != self.owner or chat.get('type') != 'private':
            return None
        if callback:
            raw = str(callback.get('data', '')).split(':')
            if len(raw) == 2 and raw[0] == 'gp' and len(raw[1]) <= 6 and raw[1].isascii() and raw[1].isdigit():
                return self._goals_reply(int(raw[1]))
            if len(raw) == 2 and raw[0] == 'm':
                screens = {'menu': self._menu_reply, 'balance': self._balance_reply,
                    'spend': self._spend_reply, 'review': self._operations_reply,
                    'goals': self._goals_reply, 'refresh': self._refresh_reply,
                    'card': self._card_reply, 'aienrich': self._ai_enrich_reply}
                screens.update({'analysis': self._analysis_reply, 'ai': self._ai_reply,
                    'settings': self._settings_reply})
                if raw[1] in screens:
                    return screens[raw[1]]()
                questions = {'changes': 'Что изменилось за неделю?',
                    'subscriptions': 'Какие подписки у меня есть?',
                    'forecast': 'Хватит ли денег до следующего поступления?',
                    'learned': 'Чему ты научился?'}
                if raw[1] in questions:
                    response = question(self.cfo, questions[raw[1]])
                    return {'text': str(response.get('text') or 'Для ответа пока недостаточно данных.'),
                        'reply_markup': self._nav()}
                if raw[1] == 'buy':
                    return {'text': '🛒 Напишите: /buy 11800 — посчитаю сценарий покупки.',
                        'reply_markup': self._nav()}
                return self._menu_reply()
            if len(raw) == 3 and raw[0] == 'gc':
                return self._confirm_group(raw[1], raw[2])
            if len(raw) == 2 and raw[0] == 'gs':
                return self._skip_group(raw[1])
            if len(raw) == 2 and raw[0] == 'on' and raw[1].isdigit():
                return self._onboarding_reply(raw[1])
            if len(raw) == 4 and raw[0] == 'cf' and raw[1].isdigit() and raw[3].isdigit():
                try:
                    saved = self.cfo.correct(int(raw[1]), raw[2], int(raw[3]))
                except ValueError:
                    saved = False
                if not saved:
                    return {'text': 'Операция уже изменилась. Откройте очередь заново.', 'reply_markup': MENU}
                buttons = [[{'text': 'Отменить подтверждение', 'callback_data': f'cu:{raw[1]}:{int(raw[3])+1}'}]]
                onboarding = self.cfo.db.get_setting('cfo.telegram_onboarding', {})
                if onboarding.get('active'):
                    buttons.append([{'text': 'Следующая группа',
                        'callback_data': f"on:{int(onboarding.get('index', 0))+1}"}])
                return {'text': 'Категория сохранена. Спасибо — пример учтён.',
                    'reply_markup': {'inline_keyboard': buttons}}
            if len(raw) == 3 and raw[0] in {'cu', 'cd'} and raw[1].isdigit() and raw[2].isdigit():
                if raw[0] == 'cu':
                    saved = undo_correction(self.cfo.db, int(raw[1]), int(raw[2]))
                    return {'text': 'Подтверждение отозвано; пример больше не используется.' if saved else 'Операция уже изменилась. Откройте /review ID.', 'reply_markup': MENU}
                with self.cfo.db.connect() as conn:
                    version = conn.execute('SELECT COALESCE(MAX(version),0) FROM category_corrections WHERE transaction_id=?', (int(raw[1]),)).fetchone()[0]
                if version != int(raw[2]):
                    return {'text': 'Операция уже изменилась. Откройте /review ID.', 'reply_markup': MENU}
                deferred = self.cfo.db.get_setting('cfo.telegram_deferred', {})
                deferred[raw[1]] = (datetime.now(UTC)+timedelta(days=1)).isoformat()
                self.cfo.db.set_setting('cfo.telegram_deferred', deferred)
                return {'text': 'Отложено на сутки. Категория не изменена.', 'reply_markup': MENU}
            if len(raw) == 2 and raw[0] == 'cg':
                draft = self.cfo.db.get_setting('cfo.telegram_goal:'+raw[1], {})
                if not draft or draft.get('used') or draft['expires'] < datetime.now(UTC).isoformat():
                    return {'text': 'Подтверждение цели истекло или уже использовано.', 'reply_markup': MENU}
                goal = GoalInput.model_validate(draft['goal'])
                existing = next((g for g in self.cfo.db.list_savings_goals(status=None) if g['name'] == goal.name), None)
                metadata = self.cfo.db.get_setting('cfo.goal_plan:'+str(existing['id']), {}) if existing else {}
                self.cfo.db.add_or_update_savings_goal(goal.name, money(goal.target_amount), money(goal.current_amount),
                    existing['priority'] if existing else goal.priority, goal.deadline,
                    notes=existing['notes'] if existing else '', status=existing['status'] if existing else goal.status,
                    metadata={**metadata, 'monthly_contribution': metadata.get('monthly_contribution', goal.monthly_contribution),
                        'updated_at': datetime.now(UTC).isoformat(), 'source': 'owner_input'})
                self.cfo.db.set_setting('cfo.telegram_goal:'+raw[1], {**draft, 'used': True})
                return {'text': 'Цель сохранена. /goals — прогресс и необходимый взнос.', 'reply_markup': MENU}
            return {'text': 'Это действие больше не поддерживается. Откройте /review.', 'reply_markup': MENU}
        text = str(message.get('text', '')).strip()
        if text in {'/start', '/help', '/menu', '🏠 Меню'}:
            return self._menu_reply()
        if text == '/money':
            return self._balance_reply()
        if text == '/expenses':
            return self._spend_reply()
        if text == '/goals':
            return self._goals_reply()
        if text.startswith('/pin'):
            # Remove the owner message before any validation or network work.
            self._delete_message(message.get('message_id'))
            match = re.fullmatch(r'/pin\s+(\d{5})', text)
            if not match:
                return {'text': 'Формат: /pin 12345 (ровно 5 цифр). Сообщение с PIN удаляется.', 'reply_markup': MENU}
            pin = match.group(1)
            with self.cfo.db.connect() as conn:
                self.cfo.db.audit(conn, 'pin_submitted_via_telegram', {'source': 'telegram'})
            try:
                result = self.cfo.refresh(allow_pin=True, pin=pin)
            except Exception as exc:  # noqa: BLE001 - never echo PIN or token-bearing errors
                return {'text': f'Не удалось выполнить авторизацию: {type(exc).__name__}. Повторно PIN не отправляйте.',
                    'reply_markup': MENU}
            finally:
                pin = ''
            if result.get('status') == 'updated':
                self._clear_sync_hold()
                bank = self.cfo.dashboard().get('bank_rub')
                return {'text': f"PIN принят. Сбер обновлён. Текущий банковский баланс: {bank+' ₽' if bank is not None else 'неизвестен'}. "
                    'Фоновое обновление разблокировано.', 'reply_markup': MENU}
            return {'text': f"PIN не принят: {result.get('status', 'unknown')}"
                + (f" ({result['code']})" if result.get('code') else '')
                + '. Повтор автоматически не выполняется; проверьте Сбер в панели.', 'reply_markup': MENU}
        if text.startswith('/profile'):
            return {'text': self._profile_reply(text[len('/profile'):].strip()), 'reply_markup': MENU}
        if text.startswith('/onboarding'):
            return self._onboarding_reply(text[len('/onboarding'):].strip())
        if text in {'/start', '/help', '🛒 Покупка'}:
            return {'text': 'Jarvis CFO · ваши деньги и решения.\n/buy 11800 — сценарий покупки\n/review [ID] — категория, происхождение и отмена\n/onboarding — стартовая группа для разметки\n/profile — резерв, защищённые средства, обязательства\n/pin 12345 — PIN Сбера (сообщение удаляется)\n/goal Название | цель | накоплено | ГГГГ-ММ-ДД — предварительный просмотр\n/advisor — финансовые вопросы\n/daily on 9 / daily off — ежедневная сводка\n/weekly on 9 — сводка по понедельникам\n/alerts on /alerts off — важные уведомления', 'reply_markup': MENU}
        if text in {'/status', '🛡 Статус'}:
            from jarvis.finance.personal_recovery import health
            state = health(self.cfo.db)
            jobs = self.cfo.db.get_setting('cfo.jobs', {})
            return {'text': f"Состояние: {state['status']} · {state.get('reason','')}\nБанк: {state.get('observed_at','нет наблюдения')}\nФоновая синхронизация: {jobs.get('sync',{}).get('status','ещё не запускалась')}\nБанк только на чтение. На выключенном ПК сервис не работает.", 'reply_markup': MENU}
        if text in {'/money', '💳 Деньги'}:
            return {'text': self.summary(), 'reply_markup': MENU}
        if text in {'/goals', '🎯 Цели'}:
            goals = self.cfo.goals(business_date(datetime.now(UTC)))
            return {'text': '\n'.join(g['name']+': '+g['current_amount']+' / '+g['target_amount']+' ₽; необходимый месячный взнос '+str((g.get('projection') or {}).get('required_monthly') or 'неизвестен')+' ₽' for g in goals) or 'Цели пока не заданы. /goal Название | цель | накоплено | ГГГГ-ММ-ДД', 'reply_markup': MENU}
        if text.startswith('/goal '):
            try:
                name, target, current, deadline = (p.strip() for p in text[6:].split('|'))
                goal = GoalInput.model_validate({'name':name, 'target_amount':target, 'current_amount':current, 'deadline':deadline})
            except ValueError:
                return {'text': 'Формат: /goal Название | 10000.00 | 1000.00 | 2027-01-01. Суммы неотрицательные.', 'reply_markup': MENU}
            payload = goal.model_dump(mode='json')
            ident = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
            self.cfo.db.set_setting('cfo.telegram_goal:'+ident, {'goal': payload, 'expires': (datetime.now(UTC)+timedelta(minutes=15)).isoformat()})
            return {'text': f'Сохранить цель «{goal.name}»: {goal.current_amount} / {goal.target_amount} ₽, срок {goal.deadline}? Переводов нет.',
                'reply_markup': {'inline_keyboard': [[{'text': 'Сохранить цель', 'callback_data': 'cg:'+ident}]]}}
        if text in {'/review', '🧾 Проверить операции'} or text.startswith('/review '):
            parts = text.split()
            if len(parts) == 2 and not parts[1].isdigit():
                return {'text': 'Формат: /review или /review ID', 'reply_markup': MENU}
            return self.review(int(parts[1]) if len(parts) == 2 else None)
        if text.split(' ', 1)[0] in {'/daily', '/weekly', '/alerts'}:
            parts = text.split(); command = parts[0][1:]
            settings = RuntimeSettings.model_validate(self.cfo.db.get_setting('cfo.runtime_settings', {}))
            if len(parts) > 1:
                valid = (len(parts) in {2, 3} and parts[1] in {'on', 'off'}
                    and (len(parts) == 2 or command != 'alerts' and parts[2].isdigit() and 8 <= int(parts[2]) <= 22))
                if not valid:
                    return {'text': 'Укажите on/off и час 8–22 для сводки. Настройки не изменены.', 'reply_markup': MENU}
                setattr(settings, command+'_enabled', parts[1] == 'on')
                if len(parts) == 3:
                    setattr(settings, command+'_hour', int(parts[2]))
                self.cfo.db.set_setting('cfo.runtime_settings', settings.model_dump())
            return {'text': f"{parts[0]}: {'включено' if getattr(settings, command+'_enabled') else 'выключено'}. Тихие часы 23:00–08:00 Москва. Важные уведомления — не чаще одного в час.", 'reply_markup': MENU}
        if text in {'⚙ Сводки'}:
            return {'text': 'Настройка: /daily on 9, /weekly on 9, /alerts on; выключить: соответствующая команда off. Weekly и alerts по умолчанию выключены. Тихие часы 23:00–08:00 Москва.', 'reply_markup': MENU}
        if text in {'🧠 Советник', '/advisor'}:
            return {'text': 'Спросите о свободных деньгах, экономии, накоплениях или покупке. Деньги считает Decimal; неизвестные обязательства не заменяются нулями. /buy 11800 — пример команды, не готовое решение. /ai — проверяемое AI-пояснение агрегатов, до 1 ₽ с кешем.', 'reply_markup': MENU}
        if text == '/ai':
            from jarvis.core.llm.orchestrator import complete_personal_analysis
            result = asyncio.run(complete_personal_analysis(self.cfo, self.cfo.db.path.parent/'cfo-ai'))
            return {'text': (result.get('text') or 'AI недоступен. Резерв при неизвестной стоимости сохраняется; повтора нет.')
                + '\nСтоимость этого обращения: ' + str(result.get('paid_cost_rub') if result.get('paid_cost_rub') is not None else 'неизвестна') + ' ₽', 'reply_markup': MENU}
        if text in {'📊 Расходы', '/expenses'}:
            text = 'Сколько я потратил за месяц?'
        try:
            value = self.cfo.purchase(text.split()[1]) if text.startswith('/buy ') else question(self.cfo, text[:1000])
        except ValueError:
            return {'text': 'Укажите положительную сумму в рублях. Например: /buy 11800', 'reply_markup': MENU}
        return {'text': value['text'], 'reply_markup': MENU}

    def poll(self) -> None:
        if not self.verify() or not self.api:
            return
        offset = self.cfo.db.get_setting('cfo.telegram_offset', None)
        failures = 0
        while not self.stop_event.is_set():
            try:
                for update in self.api.get_updates(offset, 20):
                    ident = int(update['update_id']); offset = ident+1
                    self.cfo.db.set_setting('cfo.telegram_offset', offset)
                    if not self.claim('cfo.telegram_update:'+str(ident)):
                        continue
                    if self.stop_event.is_set():
                        break
                    if update.get('message') and int(update['message'].get('date',0)) < self.started_at-300:
                        continue
                    response = self.handle(update)
                    if response:
                        photo = response.pop('photo', None)
                        if photo:
                            try:
                                result = self.api.request_photo({'chat_id': self.owner,
                                    'caption': response['text']}, photo)
                                receipt = ({'status': 'delivered', 'message_id': result['result']['message_id'],
                                    'at': datetime.now(UTC).isoformat()} if result.get('ok')
                                    else {'status': 'failed', 'at': datetime.now(UTC).isoformat()})
                            except Exception:  # noqa: BLE001 - ambiguous media is not retried
                                receipt = {'status': 'unknown'}
                        else:
                            receipt = self.deliver('reply:'+str(ident), response['text'], response.get('reply_markup'))
                        self.cfo.db.set_setting('cfo.telegram_update:'+str(ident), receipt)
                    callback = update.get('callback_query') or {}
                    if response and callback.get('id'):
                        self.api.request('answerCallbackQuery', {'callback_query_id': callback['id']})
                failures = 0
                if self.cfo.db.get_setting('cfo.telegram_state', {}).get('status') == 'network_backoff':
                    self.status('ready')
            except Exception as exc:  # noqa: BLE001 - never print token-bearing urllib errors
                failures += 1
                if getattr(exc, 'code', None) == 409:
                    self.status('poller_conflict')
                    return
                self.status('network_backoff', error_type=type(exc).__name__)
                self.stop_event.wait(min(60, 2**min(failures,6)))

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self.poll, daemon=True, name='cfo-telegram')
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
