from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from jarvis.core.llm.orchestrator import complete
from jarvis.core.llm.types import TaskType
from jarvis.finance.actions import (
    approve_action,
    plan_income_distribution,
    reject_action,
    render_action_request,
)
from jarvis.finance.context_builder import build_financial_context
from jarvis.finance.control import TRANSFER_CATEGORIES
from jarvis.finance.db import FinanceDB
from jarvis.finance.domain import Transaction
from jarvis.finance.importers import infer_category
from jarvis.finance.reports import monthly_report, render_monthly_report
from jarvis.finance.rules import advise_purchase, month_bounds
from jarvis.finance.telegram.invest_commands import InvestCommands
from jarvis.services.invest_service import InvestService

logger = logging.getLogger(__name__)

_last_request_time: dict[int, float] = {}


def _compute_backoff_delay(consecutive_errors: int) -> int:
    """Compute exponential backoff delay in seconds: 2, 4, 8, 16, 30, 30..."""
    if consecutive_errors <= 0:
        return 0
    return int(min(2 ** consecutive_errors, 30))


REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 Баланс"}, {"text": "📈 Отчёт"}],
        [{"text": "📁 Загрузить отчёт"}, {"text": "🧠 Статус AI"}],
        [{"text": "❓ Помощь"}],
    ],
    "resize_keyboard": True,
}


class TextUpdateWrapper:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.effective_user = type("User", (), {"id": user_id})()
        self.message = type("Msg", (), {
            "from_user": self.effective_user,
            "reply_text": self.reply_text,
            "document": None,
        })()
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> TextUpdateWrapper:
        self.replies.append(text)
        return self


HELP_TEXT = """AI Finance Agent v2.9.2 commands:
/start or /help - помощь
/balance - текущий баланс по локальной базе
/portfolio - текущие позиции портфеля
/news [TICKER] - новости по тикеру
/plan - финансовое предложение
/settings - текущие настройки
/bonds [N] - сканер облигаций MOEX
/risk [tickers...] - оценка риска портфеля
/status - системный healthcheck
/audit [YYYY-MM] - наблюдаемые деньги, покрытие и lineage
/export-audit [YYYY-MM] - JSON агрегатов без описаний и секретов
/account-balance <account> <currency> <YYYY-MM-DD> <amount> - остаток конца дня
/obligation <amount> <currency> <YYYY-MM-DD> - подтверждённое обязательство
/review - очередь сомнительных событий; /fix <id> <category> - коррекция
/expense <amount> [category] [description] - добавить расход
/income <amount> [description] - добавить доход
/report [YYYY-MM] - отчет за месяц
/forecast [days] - прогноз при подтверждённом покрытии
/buy <amount> <name> - проверить покупку
/distribution <income> - создать план распределения дохода
/actions - pending actions
/approve <id> - утвердить action
/reject <id> - отклонить action
/ml - ML-прогноз по категориям
/ml-info - статус AI моделей
/process - обработать загруженные PDF
/reset - сбросить незавершённую загрузку PDF
/archive-info - просмотр архива выписок
/snapshot <cash> <reserve> <savings> - сохранить остатки
/health - статус здоровья системы

Или просто напиши вопрос текстом:
"Сколько я потратил на еду за неделю?"
"""



@dataclass(frozen=True)
class TelegramConfig:
    token: str
    db_path: str
    allowed_chat_ids: set[int]
    poll_timeout: int = 30
    alerts_enabled: bool = True
    alerts_interval_minutes: int = 5
    inbox_enabled: bool = False

    @classmethod
    def from_env(cls) -> TelegramConfig:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        db_path = os.getenv("FINANCE_AGENT_DB", "./finance.db").strip() or "./finance.db"
        allowed_raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
        allowed = {int(x.strip()) for x in allowed_raw.split(",") if x.strip()}
        timeout = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30"))
        
        alerts_enabled_raw = os.getenv("TELEGRAM_ALERTS_ENABLED", "true").strip().lower()
        alerts_enabled = alerts_enabled_raw in ("1", "true", "yes", "y", "on")
        alerts_interval = int(os.getenv("TELEGRAM_ALERTS_INTERVAL_MINUTES", "5"))
        
        return cls(
            token=token, 
            db_path=db_path, 
            allowed_chat_ids=allowed, 
            poll_timeout=timeout,
            alerts_enabled=alerts_enabled,
            alerts_interval_minutes=alerts_interval
        )


class TelegramAPI:
    def __init__(self, token: str):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.file_base_url = f"https://api.telegram.org/file/bot{token}/"

    def request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(self.base_url + method, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=60) as response:
            res = json.loads(response.read().decode("utf-8"))
            return res if isinstance(res, dict) else {}

    def request_photo(self, payload: dict[str, Any], image: bytes) -> dict[str, Any]:
        """Upload an in-memory generated PNG through the existing Bot API boundary."""
        if set(payload) - {'chat_id', 'caption', 'reply_markup'}:
            raise ValueError('Closed photo fields required')
        # Reuse urllib: httpx INFO request logging would expose the token-bearing URL.
        boundary = 'jarvis-' + os.urandom(16).hex()
        chunks = []
        for key, value in payload.items():
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
        chunks.extend([(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="finance.png"\r\n'
                       'Content-Type: image/png\r\n\r\n').encode(), image, f'\r\n--{boundary}--\r\n'.encode()])
        request = urllib.request.Request(self.base_url + 'sendPhoto', data=b''.join(chunks),
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, method='POST')
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result if isinstance(result, dict) else {}

    async def answer_callback_query(self, callback_query_id: str) -> None:
        """Answer a callback query to stop the loading spinner in Telegram UI."""
        url = f"{self.base_url}answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        result = self.request("getUpdates", payload)
        if not result.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {result}")
        res_list = result.get("result", [])
        return list(res_list) if isinstance(res_list, list) else []

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        for chunk in chunk_text(text, 3500):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if reply_markup:
                payload["reply_markup"] = json.dumps(reply_markup)
            result = self.request("sendMessage", payload)
            if not result.get("ok"):
                raise RuntimeError(f"Telegram sendMessage failed: {result}")

    async def get_updates_async(self, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": max(1, min(timeout, 50))}
        if offset is not None:
            params["offset"] = int(offset)
        url = f"{self.base_url}getUpdates"
        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data}")
            res = data.get("result", [])
            return list(res) if isinstance(res, list) else []

    async def send_message_async(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        url = f"{self.base_url}sendMessage"
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            if not response.json().get("ok"):
                raise RuntimeError("Telegram rejected sendMessage")

    async def get_file_async(self, file_id: str) -> dict[str, Any]:
        url = f"{self.base_url}getFile"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json={"file_id": file_id})
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram getFile error: {data}")
            res = data.get("result", {})
            return res if isinstance(res, dict) else {}

    async def download_file_async(self, file_path: str, dest_path: str | Path) -> None:
        file_url = f"{self.file_base_url}{file_path.lstrip('/')}"
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with (
            httpx.AsyncClient(timeout=120) as client,
            client.stream("GET", file_url) as response,
        ):
            response.raise_for_status()
            content = await response.aread()
            dest.write_bytes(content)


def _extract_merchant_pattern(description: str) -> str | None:
    """Extract clean merchant name from transaction description."""
    if not description:
        return None
    
    desc = description.strip()
    
    # 1. Remove common Sber boilerplate
    clean = re.sub(
        r'\.?\s*(Операция по карте|Операция по счету|Операция по счёту)'
        r'[\s*]*\d*\s*$',
        '',
        desc,
        flags=re.IGNORECASE,
    ).strip()
    
    # 2. Remove card masks
    clean = re.sub(r'\*+\d+', '', clean)
    
    # 3. If string starts with http/https — extract domain
    url_match = re.match(r'https?://([^/\s]+)', clean, re.IGNORECASE)
    if url_match:
        domain = url_match.group(1).lower()
        # Remove common prefixes
        domain = re.sub(r'^(www\.|cabinet\.|lk\.)', '', domain)
        # Extract main name (before first dot)
        main = domain.split('.')[0]
        return main if len(main) >= 3 else None
    
    # 4. Remove all non-letters/digits at edges
    clean = re.sub(r'^[^\wА-Яа-яЁё]+|[^\wА-Яа-яЁё]+$', '', clean)
    
    # 5. Extract Latin/Cyrillic words (length >= 2), skip numbers
    words = re.findall(r'[A-Za-zА-Яа-яЁё]{2,}', clean)
    
    # 6. Filter out generic words
    GENERIC = {
        'sberbank', 'sber', 'online', 'moskva', 'moscow', 'rus',
        'russia', 'операция', 'карте', 'счету', 'платежного',
        'перевод', 'sbp', 'sbol',
    }
    words = [w for w in words if w.lower() not in GENERIC]
    
    if not words:
        return None
    
    # Return first 2 meaningful words, joined by space
    return ' '.join(words[:2]).lower()


class FinanceTelegramBot:
    def __init__(self, db: FinanceDB, token: str = "", alerts_mgr: Any = None):
        self.db = db
        self.db.init()
        self.alerts_mgr = alerts_mgr
        self.invest = InvestCommands(InvestService(), self.db)
        if not token:
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if token:
            self.api: TelegramAPI | None = TelegramAPI(token)
        else:
            self.api = None

    async def handle_free_text(self, text: str) -> str:
        try:
            context = build_financial_context(self.db)
            if len(context) > 3000:
                context = context[:3000]
            
            system_prompt = (
                "Ты финансовый советник студента. "
                "Отвечай КОРОТКО (2-4 строки), на русском. "
                "В контексте есть данные: "
                "РАСХОДЫ ЗА ПОСЛЕДНИЕ 7 ДНЕЙ, "
                "РАСХОДЫ ЗА ПОСЛЕДНИЕ 30 ДНЕЙ, "
                "ПОСЛЕДНИЕ 15 ТРАНЗАКЦИЙ. "
                "Используй ТОЛЬКО данные из контекста. "
                "Если спрашивают про 'за неделю' — используй блок 7 ДНЕЙ. "
                "Если 'в этом месяце' — используй 30 ДНЕЙ. "
                "НЕ пиши свои мысли, сразу давай ответ."
            )
            
            full_prompt = f"{system_prompt}\n\nКонтекст:\n{context}\n\nВопрос: {text}"
            
            result = await complete(
                task=TaskType.OTHER,
                prompt=full_prompt,
            )
            answer = result.content
            
            if answer and answer.strip():
                return answer.strip()
            logger.warning("LLM returned empty, using rule-based")
        except Exception as e:
            logger.exception("handle_free_text failed")
            print(f"[BOT] handle_free_text ERROR: {e}")
        
        return self._rule_based_answer(text)

    def _rule_based_answer(self, text: str) -> str:
        text_lower = text.lower()
        
        # Balance
        if any(k in text_lower for k in ("баланс", "сколько денег", "сколько у меня")):
            snap = self.db.latest_budget_snapshot()
            if snap:
                cash = float(snap.get("cash_balance", 0))
                res = float(snap.get("reserve_balance", 0))
                sav = float(snap.get("savings_balance", 0))
                return (
                    f"💰 Баланс по счетам:\n"
                    f"Основной: {cash:.2f} ₽\n"
                    f"Резерв: {res:.2f} ₽\n"
                    f"Копилка: {sav:.2f} ₽\n"
                    f"Итого: {cash + res + sav:.2f} ₽"
                )
        
        # Food last 7 days
        if any(k in text_lower for k in ("еда", "еду", "еды", "поел", "покушать")):
            cutoff = (datetime.now(UTC).date() - timedelta(days=7)).isoformat()
            txs = [
                t for t in self.db.all_transactions()
                if float(t.get('amount', 0)) < 0
                and str(t.get('tx_date', '')) >= cutoff
                and t.get('category') in ('food', 'food_basic', 'delivery', 'cafes')
                and t.get('category') not in TRANSFER_CATEGORIES
            ]
            total = sum(abs(float(t['amount'])) for t in txs)
            
            by_cat: dict[str, float] = {}
            for t in txs:
                cat = str(t.get('category', 'other'))
                by_cat[cat] = by_cat.get(cat, 0.0) + abs(float(t.get('amount', 0)))
            detail = "\n".join(f"  {c}: {v:.0f} ₽" for c, v in sorted(by_cat.items(), key=lambda x: -x[1]))
            
            return f"🍽️ На еду за 7 дней: {total:.0f} ₽\n{detail}"
        
        # Overspend
        if any(k in text_lower for k in ("перерасход", "много трачу", "превыш")):
            from jarvis.finance.control import build_control_report
            report = build_control_report(self.db)
            red = [r for r in report.category_rows if r.monthly_spend > r.current_limit and r.current_limit > 0]
            if red:
                lines = [f"🔴 {r.category}: {r.monthly_spend:.0f} / {r.current_limit:.0f} ₽" for r in red[:5]]
                return "Перерасход по категориям:\n" + "\n".join(lines)
            return "✅ Перерасходов нет — все категории в пределах лимитов."
        
        # Can I buy
        if any(k in text_lower for k in ("могу ли", "можно ли", "купить")):
            return (
                "Для ответа на этот вопрос введи команду:\n"
                "/buy <amount> <название>\n"
                "Например: /buy 5000 наушники"
            )
        
        # Default
        return (
            "Не могу ответить (AI вернул пусто). Попробуй команды:\n"
            "/balance — баланс\n"
            "/report 2026-09 — отчёт\n"
            "/forecast 14 — прогноз\n"
            "Или переформулируй вопрос."
        )

    async def handle_text_async(self, text: str, chat_id: int = 0) -> str:
        text = text.strip()
        if not text:
            return HELP_TEXT

        # Persistent menu button text aliases
        if text == "📊 Баланс":
            text = "/balance"
        elif text == "📈 Отчёт":
            text = "/report"
        elif text == "📁 Загрузить отчёт":
            return (
                "📁 Загрузка выписок Sber (PDF):\n\n"
                "1. Отправь 4 PDF файла выписок в чат (3 по счетам + 1 остатки).\n"
                "2. Бот покажет статус загрузки [1/4], [2/4]...\n"
                "3. Напиши /process для read-only проверки и сохранения в карантине.\n\n"
                "Дополнительные команды:\n"
                "/process — проверить файлы без изменения финансовой БД\n"
                "/reset — сбросить файлы текущей сессии\n"
                "/archive-info — просмотреть архив прошлых выписок"
            )
        elif text == "🧠 Статус AI":
            text = "/ml-info"
        elif text == "❓ Помощь":
            text = "/help"

        # Rate limit check for free-text AI messages (slash commands bypass rate limit)
        if not text.startswith("/"):
            now = time.time()
            last = _last_request_time.get(chat_id, 0.0)
            if now - last < 3:
                return "Подожди 3 секунды."
            _last_request_time[chat_id] = now

        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]

        try:
            if command in {"/start", "/help", "help"}:
                return HELP_TEXT
            if command in {"/audit", "/export-audit"}:
                from jarvis.finance.audit_report import (
                    build_audit_report,
                    render_audit_report,
                )
                month = args[0] if args else datetime.now(UTC).strftime("%Y-%m")
                start, end = month_bounds(month)
                audit_data = build_audit_report(self.db, start, end)
                return json.dumps(audit_data, ensure_ascii=False, indent=2) if command == "/export-audit" else render_audit_report(audit_data)
            if command == "/account-balance":
                if len(args) != 4:
                    return "Использование: /account-balance <account> <currency> <YYYY-MM-DD> <amount>"
                self.db.record_account_balance(args[0], args[1].upper(), datetime.fromisoformat(args[2]).date(), money(args[3]))
                return "Остаток конца дня сохранён с source=manual; он не заменяет банковскую сверку."
            if command == "/obligation":
                if len(args) != 3:
                    return "Использование: /obligation <amount> <currency> <YYYY-MM-DD>"
                amount = money(args[0])
                if amount <= 0:
                    raise ValueError("Обязательство должно иметь положительную сумму")
                due = datetime.fromisoformat(args[2]).date().isoformat()
                obligations = self.db.get_setting("obligations", [])
                obligations.append({"id": len(obligations) + 1, "amount": str(amount), "currency": args[1].upper(), "due_date": due, "source": "manual"})
                self.db.set_setting("obligations", obligations)
                return "Подтверждённое обязательство сохранено."
            if command == "/review":
                with self.db.connect() as conn:
                    rows = conn.execute("SELECT id,source,transaction_id FROM source_events WHERE status='pending_review' ORDER BY id DESC LIMIT 20").fetchall()
                    duplicates = conn.execute("SELECT count(*) FROM (SELECT account,external_id FROM transactions WHERE external_id IS NOT NULL GROUP BY account,external_id HAVING count(*)>1)").fetchone()[0]
                lines = [f"Pending review: {len(rows)} показано; legacy ID групп: {duplicates}"]
                lines.extend(f"Событие #{row['id']}: source={row['source']}, tx #{row['transaction_id']}" for row in rows)
                return "\n".join(lines) + "\n/fix <tx_id> <category> исправляет метку с сохранением оригинала; денежные конфликты требуют исходной выписки."
            if command == "/balance":
                if self.invest.service.sber_configured():
                    return self.invest.handle_balance()
                snapshot = self.db.latest_budget_snapshot()
                if snapshot:
                    cash = float(snapshot.get("cash_balance", 0))
                    reserve = float(snapshot.get("reserve_balance", 0))
                    savings = float(snapshot.get("savings_balance", 0))
                    return (
                        f"💰 Баланс по snapshot {snapshot['snapshot_date']} (локальный источник):\n"
                        f"Основной счёт: {cash:.2f} RUB\n"
                        f"Резерв: {reserve:.2f} RUB\n"
                        f"Копилка: {savings:.2f} RUB\n"
                        f"Итого: {cash+reserve+savings:.2f} RUB"
                    )
                else:
                    return f"Баланс банка: неизвестен. Сумма записанных движений: {self.db.balance()} RUB; snapshot не найден. /audit показывает покрытие."
            if command == "/operations":
                days = int(args[0]) if args and args[0].isdigit() else 7
                return self.invest.handle_operations(days=days)
            if command == "/market":
                symbol = args[0] if args else "SBER"
                return self.invest.handle_market(symbol)
            if command == "/scan":
                top_n = int(args[0]) if args and args[0].isdigit() else 10
                return self.invest.handle_scan(top_n=top_n)
            if command == "/bonds":
                top_n = int(args[0]) if args and args[0].isdigit() else 10
                return self.invest.handle_bonds(top_n=top_n)
            if command == "/risk":
                return self.invest.handle_risk(args if args else None)
            if command == "/stress":
                scenario = args[0] if args else "2008_crisis"
                return self.invest.handle_stress(scenario)
            if command == "/portfolio":
                return "📊 Портфель: нет активного подключения источника позиций. /balance — банковские счета."
            if command == "/news":
                ticker = args[0].upper() if args else "ALL"
                return f"📰 Новости ({ticker}): источник новостей не подключён. См. FUTURE_PLANS.md слой 7.1."
            if command == "/plan":
                try:
                    from jarvis.bridge.free_cash import get_free_cash

                    get_free_cash(self.db)  # Refuse before any scanner/live source access.
                    from jarvis.bridge.proposal_engine import (
                        build_proposal,
                        render_proposal,
                    )
                    from jarvis.invest.scanner.stock_scanner import scan_stocks

                    top_stocks = []
                    if self.invest.service._moex:
                        try:
                            scanned = scan_stocks(self.invest.service._moex, top_n=3)
                            top_stocks = [{"symbol": getattr(s, "ticker", ""), "price": getattr(s, "price", 0.0), "reason": f"Sharpe {getattr(s, 'sharpe', 0.0):.2f}"} for s in scanned]
                        except Exception:  # noqa: BLE001, S110
                            pass
                    tp = build_proposal(self.db, [], top_stocks)
                    if tp:
                        return render_proposal(tp)
                    return "💡 План: нет свободных средств или условий для генерации предложения."
                except Exception as e:  # noqa: BLE001
                    return f"💡 План: недоступно ({e})"
            if command == "/settings":
                token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                tok_short = "configured" if token else "not_set"
                rules = load_rules(self.db)
                risk_profile = rules.get("risk_profile", "moderate")
                alerts = "enabled" if self.alerts_mgr and self.alerts_mgr.state.get("enabled", True) else "disabled"
                return (
                    "⚙️ *Настройки*\n"
                    f"• Риск-профиль: {risk_profile}\n"
                    f"• Уведомления: {alerts}\n"
                    f"• Bot Token: `{tok_short}`\n"
                    f"• Scheduler: не проверен (используйте /status)"
                )
            if command == "/free":
                return self.invest.handle_free()
            if command == "/optimal":
                return self.invest.handle_optimal()
            if command == "/help_invest":
                return self.invest.handle_help()
            if command == "/expense":
                return self._expense(args)
            if command == "/income":
                return self._income(args)
            if command == "/report":
                return self._report(args)
            if command == "/forecast":
                return self._forecast(args)
            if command == "/health":
                return await self._health(args)
            if command == "/status":
                from jarvis.finance.scheduler.jobs import job_healthcheck
                res = job_healthcheck(str(self.db.path))
                return f"🏥 Status: {res['status']}\nDetails: {json.dumps(res['details'], ensure_ascii=False)}"
            if command == "/rag_test":
                from jarvis.finance.classification.evaluation import confirmed_examples
                count = len(confirmed_examples(self.db.path))
                return (f"RAG: подтверждённых меток {count}. Независимая метрика не проверена. "
                        "Оценка: scripts/test_rag_classifier.py --db PATH --cutoff YYYY-MM-DD; "
                        "тестовый период и мерчанты исключаются из отдельного индекса.")
            if command == "/insights":
                return await self._insights(args)
            if command == "/incoming":
                return self._incoming(args)
            if command == "/confirm_income":
                return self._confirm_income(args)
            if command == "/reject_income":
                return self._reject_income(args)
            if command == "/set_parent":
                return self._set_parent(args)
            if command == "/confirm_all_unknown_as":
                return self._confirm_all_unknown_as(args)

            if command == "/buy":
                return self._buy(args)
            if command == "/distribution":
                return self._distribution(args)
            if command == "/actions":
                return self._actions()
            if command == "/approve":
                return self._approve(args)
            if command == "/reject":
                return self._reject(args)
            if command == "/ml":
                return self._ml()
            if command in {"/ml-info", "/ml_info"}:
                return self._ml_info()
            if command == "/drift":
                from jarvis.finance.ml_local.drift_detector import (
                    detect_drift,
                    render_drift_report,
                )
                report = detect_drift(self.db, window_days=7)
                return render_drift_report(report)
            if command == "/process":
                from jarvis.finance.telegram.upload_handler import process_command
                wrapper = TextUpdateWrapper(chat_id)
                await process_command(wrapper)
                return "\n\n".join(wrapper.replies) if wrapper.replies else "Обработано."
            if command in {"/force_process", "/forceprocess"}:
                from jarvis.finance.telegram.upload_handler import force_process_command
                wrapper = TextUpdateWrapper(chat_id)
                await force_process_command(wrapper)
                return "\n\n".join(wrapper.replies) if wrapper.replies else "Обработано принудительно."
            if command == "/reset":
                from jarvis.finance.telegram.upload_handler import reset_command
                wrapper = TextUpdateWrapper(chat_id)
                await reset_command(wrapper)
                return "\n\n".join(wrapper.replies) if wrapper.replies else "Сброшено."
            if command == "/archive-info":
                from jarvis.finance.telegram.upload_handler import archive_info_command
                wrapper = TextUpdateWrapper(chat_id)
                await archive_info_command(wrapper)
                return "\n\n".join(wrapper.replies) if wrapper.replies else "Архив."
            if command == "/snapshot":
                return self._snapshot(args)
            if command == "/alerts_on":
                if self.alerts_mgr:
                    self.alerts_mgr.state["enabled"] = True
                    self.alerts_mgr._save_state()
                    return "Уведомления включены"
                return "Модуль уведомлений не настроен."
            if command == "/alerts_off":
                if self.alerts_mgr:
                    self.alerts_mgr.state["enabled"] = False
                    self.alerts_mgr._save_state()
                    return "Уведомления выключены"
                return "Модуль уведомлений не настроен."
            if command == "/alerts_test":
                if self.alerts_mgr:
                    alerts = self.alerts_mgr.check_all()
                    if not alerts:
                        return "Нет сработавших уведомлений."
                    return "Тестовые уведомления:\n\n" + "\n\n".join(alerts)
                return "Модуль уведомлений не настроен."
            if command == "/learn":
                return self._cmd_learn(args)
            if command == "/rules":
                return self._cmd_rules()
            if command == "/forget":
                return self._cmd_forget(args)
            if command == "/fix":
                return self._cmd_fix(args)
            if command == "/onboarding":
                import sqlite3

                from jarvis.finance.onboarding.queue_builder import (
                    build_queue_from_unclassified,
                    get_next_question,
                )
                if args and args[0] == "--rebuild":
                    added = build_queue_from_unclassified(str(self.db.path))
                    return f"🔄 Очередь пересоздана: +{added} вопросов."
                if args and args[0] == "--stats":
                    conn = sqlite3.connect(str(self.db.path))
                    try:
                        in_p = conn.execute("SELECT COUNT(1) FROM known_senders WHERE times_confirmed >= 1").fetchone()[0]
                        out_p = conn.execute("SELECT COUNT(1) FROM outgoing_patterns WHERE times_confirmed >= 1").fetchone()[0]
                        pending = conn.execute("SELECT COUNT(1) FROM question_queue WHERE status = 'pending'").fetchone()[0]
                    except Exception:  # noqa: BLE001
                        in_p, out_p, pending = 0, 0, 0
                    conn.close()
                    return f"📊 Onboarding Stats:\n• Incoming patterns: {in_p}\n• Outgoing patterns: {out_p}\n• Pending questions: {pending}"

                next_q = get_next_question(str(self.db.path))
                if not next_q:
                    return "🎉 Вопросов нет! Используй /onboarding --rebuild для сканирования."

                keyboard = []
                for s in next_q.get("suggestions", []):
                    keyboard.append([{"text": s["label"], "callback_data": f"onb:{next_q['id']}:{s['value']}"}])
                keyboard.append([{"text": "⏭️ Пропустить", "callback_data": f"onb:{next_q['id']}:skip"}])
                try:
                    if self.api:
                        await self.api.send_message_async(chat_id, next_q["text"], reply_markup={"inline_keyboard": keyboard})
                    return "Отправил вопрос выше 👆"
                except Exception as e:  # noqa: BLE001
                    return f"Ошибка отправки вопроса: {e}"

            if command in {"/reject_income", "/rejectincome"}:
                if not args:
                    return "Использование: /reject_income <id>"
                try:
                    tx_id = int(args[0])
                except ValueError:
                    return "id должен быть числом"
                import sqlite3
                conn = sqlite3.connect(str(self.db.path))
                conn.execute("UPDATE incoming_classifications SET status = 'rejected' WHERE income_tx_id = ?", (tx_id,))
                conn.execute("INSERT INTO classification_rejections (target_id, wrong_category, created_at) VALUES (?, 'incoming', datetime('now'))", (tx_id,))
                conn.commit()
                conn.close()
                return f"❌ Перевод #{tx_id} отклонён."
            if command == "/notifications":
                return await self._notifications(args)
        except (InvalidOperation, ValueError) as exc:
            return f"Ошибка формата: {exc}"
            
        if command.startswith("/"):
            return "Неизвестная команда. Напиши /help."
        return await self.handle_free_text(text)

    def _cmd_learn(self, args: list[str]) -> str:
        if len(args) < 2:
            return "Использование: /learn <паттерн> <категория>\nПример: /learn funpay games_digital"
        pattern = args[0]
        category = args[1]
        rule_id = self.db.add_merchant_rule(pattern, category, source="manual")
        return f"✅ Правило добавлено (id={rule_id}): '{pattern}' → {category}"

    def _cmd_rules(self) -> str:
        rules = self.db.list_merchant_rules(limit=30)
        if not rules:
            return "Правил пока нет. Добавь через /learn <паттерн> <категория>"
        lines = [f"📋 Merchant Rules ({len(rules)}):"]
        for r in rules:
            lines.append(f"  #{r['id']}: '{r['pattern']}' → {r['category']} (×{r['hits']})")
        return "\n".join(lines)

    def _cmd_forget(self, args: list[str]) -> str:
        if not args:
            return "Использование: /forget <rule_id>"
        try:
            rid = int(args[0])
        except ValueError:
            return "rule_id должен быть числом"
        if self.db.delete_merchant_rule(rid):
            return f"✅ Правило #{rid} удалено"
        return f"❌ Правило #{rid} не найдено"

    def _cmd_fix(self, args: list[str]) -> str:
        if len(args) < 2:
            return "Использование: /fix <tx_id> <category>"
        try:
            tx_id = int(args[0])
        except ValueError:
            return "tx_id должен быть числом"
        category = args[1]
        
        # Update category
        ok = self.db.update_transaction_category(tx_id, category, source="manual")
        if not ok:
            return f"❌ Транзакция #{tx_id} не найдена"
        
        # Auto-learn: extract merchant pattern from description
        txs = self.db.all_transactions()
        target = next((t for t in txs if t.get('id') == tx_id), None)
        if target:
            pattern = _extract_merchant_pattern(str(target.get('description', '')))
            if pattern:
                self.db.add_merchant_rule(pattern, category, source="ai_correction")
                return (
                    f"✅ Транзакция #{tx_id} → {category}\n"
                    f"🧠 Авто-правило: '{pattern}' → {category}"
                )
        return f"✅ Транзакция #{tx_id} → {category}"

    def _expense(self, args: list[str]) -> str:
        if not args:
            raise ValueError("используй /expense <amount> [category] [description]")
        amount = money(args[0])
        if amount <= 0:
            raise ValueError("amount должен быть положительным числом")
        category = args[1] if len(args) >= 2 else ""
        description = " ".join(args[2:]) if len(args) >= 3 else "Manual expense"
        if not category:
            category = infer_category(description, "", -amount)
        tx_id = self.db.add_transaction(
            Transaction(
                tx_date=datetime.now(UTC).date(),
                amount=-amount,
                currency="RUB",
                description=description,
                merchant="telegram",
                category=category,
                account="main",
            ),
            source="telegram",
        )
        return f"Расход добавлен: -{amount} RUB, категория {category}, tx_id={tx_id}."

    def _income(self, args: list[str]) -> str:
        if not args:
            raise ValueError("используй /income <amount> [description]")
        amount = money(args[0])
        if amount <= 0:
            raise ValueError("amount должен быть положительным числом")
        description = " ".join(args[1:]) if len(args) >= 2 else "Manual income"
        tx_id = self.db.add_transaction(
            Transaction(
                tx_date=datetime.now(UTC).date(),
                amount=amount,
                currency="RUB",
                description=description,
                merchant="telegram",
                category="income",
                account="main",
            ),
            source="telegram",
        )
        return f"Доход добавлен: +{amount} RUB, tx_id={tx_id}."

    def _report(self, args: list[str]) -> str:
        month_str = args[0] if args else datetime.now(UTC).strftime("%Y-%m")
        rules = load_rules(self.db)
        start, end = month_bounds(month_str)
        transactions = self.db.transactions_between(start, end)
        
        INCOME_EXCLUDED = {
            "internal_transfer",
            "savings_transfer",
            "transfer",
        }
        
        filtered_txs = []
        for t in transactions:
            amt = Decimal(str(t.get("amount", 0)))
            cat = str(t.get("category", "")).lower()
            if amt > 0 and cat in INCOME_EXCLUDED:
                continue
            filtered_txs.append(t)
            
        rep = monthly_report(filtered_txs, month_str, rules)
        return render_monthly_report(rep)

    def _forecast(self, args: list[str]) -> str:
        from jarvis.finance.audit_report import build_audit_report, liquidity_forecast
        if len(args) > 1:
            return "Использование: /forecast [days]; ожидаемый доход не считается подтверждённым остатком."
        days = int(args[0]) if args else 14
        if not 1 <= days <= 90:
            raise ValueError("Горизонт должен быть от 1 до 90 дней")
        today = datetime.now(UTC).date()
        report = build_audit_report(self.db, today - timedelta(days=27), today)
        result = report["forecast"]
        if result["status"] == "insufficient_data":
            return "Прогноз недоступен: нужны 28 дней подтверждённого покрытия и сверенный остаток. /audit показывает пробелы."
        rows = self.db.transactions_between(today - timedelta(days=27), today)
        from jarvis.finance.ledger import cashflow
        daily = [float(cashflow([row for row in rows if row['tx_date'] == (today - timedelta(days=27-i)).isoformat()])["movement"]) for i in range(28)]
        closing = sum((Decimal(item["reconciliation"]["closing"]) for item in report["accounts"]), Decimal(0))
        result = liquidity_forecast(daily, closing, horizon=days)
        return "Экспериментальный прогноз (будущие обязательства показаны отдельно):\n" + json.dumps(result, ensure_ascii=False)

    async def _health(self, args: list[str]) -> str:
        from jarvis.finance.readiness import local_readiness, render_readiness

        return render_readiness(local_readiness(self.db.path))

    async def _insights(self, args: list[str]) -> str:
        from jarvis.finance.insights import get_advice, render_insights
        from jarvis.finance.insights.reporter import build_insight_report
        days = int(args[0]) if args else 30
        rep = build_insight_report(str(self.db.path), period_days=days)
        adv = await get_advice(rep.insights)
        rep.llm_summary = adv
        return render_insights(rep, max_insights=5, format_mode="telegram")

    def _incoming(self, args: list[str]) -> str:
        from jarvis.finance.incoming.classifier import process_all_unclassified_incoming
        count = process_all_unclassified_incoming(str(self.db.path))
        if not count:
            return "Нет новых переводов для обработки."
        return f"Обработано {count} переводов."

    def _confirm_income(self, args: list[str]) -> str:
        if not args:
            return "Использование: /confirm_income <tx_id>"
        try:
            tx_id = int(args[0])
        except ValueError:
            return "tx_id должен быть числом"
        from jarvis.finance.incoming.learner import confirm_classification
        confirm_classification(str(self.db.path), tx_id, transfer_type="income")
        return f"✅ Классификация перевода #{tx_id} подтверждена."

    def _reject_income(self, args: list[str]) -> str:
        if not args:
            return "Использование: /reject_income <tx_id>"
        try:
            tx_id = int(args[0])
        except ValueError:
            return "tx_id должен быть числом"
        import sqlite3
        conn = sqlite3.connect(str(self.db.path))
        conn.execute("UPDATE incoming_classifications SET status = 'rejected' WHERE income_tx_id = ?", (tx_id,))
        conn.commit()
        conn.close()
        return f"❌ Перевод #{tx_id} отклонён."

    def _set_parent(self, args: list[str]) -> str:
        if len(args) < 2:
            return "Использование: /set_parent <sender_pattern> <category>"
        return "⚠️ Ручная установка родительского отправителя временно не поддерживается."

    def _confirm_all_unknown_as(self, args: list[str]) -> str:
        if not args:
            return "Использование: /confirm_all_unknown_as <category>"
        return "⚠️ Подтверждение всех неизвестных категорией временно не поддерживается."

    def _buy(self, args: list[str]) -> str:
        if len(args) < 2:
            raise ValueError("используй /buy <amount> <name>")
        amount = money(args[0])
        name = " ".join(args[1:])
        rules = load_rules(self.db)
        balance = self.db.balance()
        decision = advise_purchase(name=name, amount=amount, current_balance=balance, rules=rules)
        status = "МОЖНО БРАТЬ" if decision.can_buy else "ЛУЧШЕ ПОДОЖДАТЬ"
        return f"{status}: {decision.message}"

    def _distribution(self, args: list[str]) -> str:
        if not args:
            raise ValueError("используй /distribution <income>")
        income = money(args[0])
        if income <= 0:
            raise ValueError("income должен быть положительным числом")
        rules = load_rules(self.db)
        action_id = plan_income_distribution(self.db, income=income, rules=rules)
        req = self.db.get_action_request(action_id)
        if req:
            return f"Создан план #{action_id}:\n{render_action_request(req)}"
        return f"Создан план #{action_id}."

    def _actions(self) -> str:
        pending = self.db.list_action_requests(status="pending")
        if not pending:
            return "Нет ожидающих action requests."
        lines = ["Pending action requests:"]
        for r in pending:
            lines.append(f"Action #{r.id}: {r.action_type} - {r.reason}")
        return "\n".join(lines)

    def _approve(self, args: list[str]) -> str:
        if not args:
            raise ValueError("используй /approve <action_id>")
        action_id = int(args[0])
        _ok, msg = approve_action(self.db, action_id)
        return msg

    def _reject(self, args: list[str]) -> str:
        if not args:
            raise ValueError("используй /reject <action_id>")
        action_id = int(args[0])
        _ok, msg = reject_action(self.db, action_id)
        return msg

    def _ml(self) -> str:
        from jarvis.finance.ml_local.spend_predictor import (
            build_spend_forecast,
            render_spend_forecast,
        )
        rules = load_rules(self.db)
        forecast_data = build_spend_forecast(self.db, rules=rules)
        return render_spend_forecast(forecast_data)

    def _ml_info(self) -> str:
        from jarvis.finance.ml_local.local_categorizer import get_local_categorizer
        cat = get_local_categorizer()
        status = "Доступен" if cat.is_available else "Недоступен"
        return f"AI Status: {status}"

    def _snapshot(self, args: list[str]) -> str:
        if len(args) < 3:
            return "Использование: /snapshot <cash> <reserve> <savings>\nПример: /snapshot 15000 5000 2000"
        try:
            cash = Decimal(args[0].replace(",", "."))
            reserve = Decimal(args[1].replace(",", "."))
            savings = Decimal(args[2].replace(",", "."))
        except InvalidOperation:
            return "Ошибка: суммы должны быть числами"
        sid = self.db.add_budget_snapshot(
            snapshot_date=datetime.now(UTC).date(),
            cash_balance=cash,
            reserve_balance=reserve,
            savings_balance=savings,
        )
        return f"✅ Остатки сохранены (snapshot #{sid}):\nОсновной: {cash:.2f} ₽\nРезерв: {reserve:.2f} ₽\nКопилка: {savings:.2f} ₽\nИтого: {cash+reserve+savings:.2f} ₽"

    async def _notifications(self, args: list[str]) -> str:
        from jarvis.finance.notifications.dispatcher import (
            dispatch_notifications,
        )
        sent = dispatch_notifications(str(self.db.path))
        return f"🔔 Диспетчер уведомлений: отправлено {sent} сообщений."


async def _handle_callback_query(callback: dict[str, Any], bot: FinanceTelegramBot, db_path: str) -> None:
    cb_id = callback.get("id")
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    if cb_id and bot.api:
        try:
            await bot.api.answer_callback_query(cb_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to answer callback query: {e}")

    if data.startswith("onb:"):
        from jarvis.finance.onboarding.responder import apply_answer
        parts = data.split(":", 2)
        if len(parts) == 3:
            q_id_str, user_val = parts[1], parts[2]
            try:
                q_id = int(q_id_str)
                res = apply_answer(db_path, q_id, user_val)
                res_text = f"Ответ принят ({res.get('reason', 'ok')})" if res.get("applied") else f"Пропущено: {res.get('reason')}"
                if chat_id and bot.api:
                    await bot.api.send_message_async(chat_id, res_text)
            except Exception as e:  # noqa: BLE001
                if chat_id and bot.api:
                    await bot.api.send_message_async(chat_id, f"❌ Ошибка обработки: {e}")


async def _run_polling_async(config: TelegramConfig) -> None:
    from jarvis.finance.telegram.alerts import AlertsManager
    
    if not config.token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN in environment or .env")
    if not config.allowed_chat_ids:
        raise ValueError("Set TELEGRAM_ALLOWED_CHAT_IDS before exposing financial commands")
    
    db = FinanceDB(config.db_path)
    alerts_mgr = AlertsManager(db, state_file=".alerts_state.json") if config.alerts_enabled else None
    bot = FinanceTelegramBot(db, config.token, alerts_mgr=alerts_mgr)
    offset: int | None = None
    
    last_alerts_check = datetime.now(UTC)
    alerts_interval = timedelta(minutes=config.alerts_interval_minutes)

    consecutive_network_errors = 0
    MAX_NETWORK_ERRORS_BEFORE_ALERT = 5
    admin_chat_id = next(iter(config.allowed_chat_ids), None) if config.allowed_chat_ids else None
    
    from jarvis.finance.scheduler.service import (
        SchedulerService,
    )

    def _telegram_broadcast(text: str) -> None:
        """Send text to all allowed chats synchronously from scheduler thread."""
        for cid in config.allowed_chat_ids:
            try:
                if bot.api:
                    asyncio.run_coroutine_threadsafe(
                        bot.api.send_message_async(cid, text),
                        asyncio.get_event_loop(),
                    )
            except Exception:
                logger.exception(f"Broadcast failed for {cid}")

    _sched_enabled = os.getenv("TELEGRAM_SCHEDULER_ENABLED", "false").strip().lower() in ("1", "true", "yes", "y", "on")
    if _sched_enabled:
        scheduler = SchedulerService(config.db_path, telegram_sender=_telegram_broadcast)
        scheduler.start()
        print(f"[BOT] Scheduler started: {scheduler.list_jobs()}")
    else:
        print("[BOT] Scheduler disabled (TELEGRAM_SCHEDULER_ENABLED=false). Use Task Scheduler.")

    print(f"[BOT] Polling started. Send /start in Telegram. Alerts: {config.alerts_enabled}")
    logger.info("Bot polling started")
    
    while True:
        try:
            if alerts_mgr and alerts_mgr.state.get("enabled", True) and datetime.now(UTC) - last_alerts_check >= alerts_interval:
                last_alerts_check = datetime.now(UTC)
                alerts = alerts_mgr.check_all()
                for alert in alerts:
                    for chat_id in config.allowed_chat_ids:
                        try:
                            if bot.api:
                                await bot.api.send_message_async(chat_id, alert)
                                print(f"[BOT] Alert sent to {chat_id}: {alert[:80]}")
                        except Exception:
                            logger.exception("Alert send failed")

            if bot.api:
                updates = await bot.api.get_updates_async(
                    offset=offset, 
                    timeout=config.poll_timeout,
                )
            else:
                updates = []
            if consecutive_network_errors > 0:
                logger.info("Network restored after %d failed attempts", consecutive_network_errors)
                print(f"[BOT] Network restored (was down for {consecutive_network_errors} attempts)")
                consecutive_network_errors = 0

            for update in updates:
                update_id = update.get("update_id")
                offset = int(update_id) + 1 if update_id else offset
                
                # Handle callback queries (e.g., onboarding inline buttons)
                callback = update.get("callback_query")
                if callback:
                    await _handle_callback_query(callback, bot, str(db.path))
                    continue

                message = update.get("message") or update.get("edited_message") or {}
                text = (message.get("text") or "").strip()
                document = message.get("document")
                chat = message.get("chat") or {}
                raw_chat_id = chat.get("id")
                
                if raw_chat_id is None:
                    continue
                chat_id = int(raw_chat_id)

                if config.allowed_chat_ids and chat_id not in config.allowed_chat_ids:
                    if bot.api:
                        await bot.api.send_message_async(chat_id, "Этот chat_id не разрешен для AI Finance Agent.")
                    continue

                if document:
                    try:
                        file_id = document.get("file_id")
                        raw_file_name = document.get("file_name", "unknown.pdf") or "unknown.pdf"
                        file_size = document.get("file_size", 0)

                        file_name = raw_file_name.replace("\xa0", " ").replace("\u202f", " ").replace("/", "_").replace("\\", "_")

                        from jarvis.finance.telegram.upload_handler import (
                            handle_document,
                        )

                        async def _save_to(*args: Any, current_file_id: str | None = file_id, **kwargs: Any) -> None:
                            dest_path = Path(args[-1])
                            if not current_file_id:
                                raise RuntimeError("Missing file_id in document update")
                            if bot.api:
                                info = await bot.api.get_file_async(current_file_id)
                                tg_path = info.get("file_path")
                                if not tg_path:
                                    raise RuntimeError(f"Could not resolve file_path for file_id={current_file_id}")
                                await bot.api.download_file_async(tg_path, dest_path)

                        doc_wrapper = type("Doc", (), {
                            "file_name": file_name,
                            "file_size": file_size,
                            "save_to": _save_to,
                        })()

                        wrapper = TextUpdateWrapper(chat_id)
                        wrapper.message.document = doc_wrapper

                        await handle_document(wrapper)
                        reply = "\n\n".join(wrapper.replies) if wrapper.replies else "Файл сохранён."
                        if bot.api:
                            await bot.api.send_message_async(chat_id, reply, reply_markup=REPLY_KEYBOARD)
                    except Exception as e:
                        logger.exception("Document download/handle failed")
                        if bot.api:
                            await bot.api.send_message_async(chat_id, f"❌ Ошибка загрузки файла: {e}", reply_markup=REPLY_KEYBOARD)
                    continue

                if not text:
                    continue
                
                print(f"[BOT] Received: {text[:80]}")
                logger.info(f"Received from {chat_id}: {text[:80]}")
                
                try:
                    reply = await bot.handle_text_async(text, chat_id=chat_id)
                    if not reply:
                        reply = "Пустой ответ."
                    print(f"[BOT] Reply: {reply[:120]}")
                    markup = REPLY_KEYBOARD if text in {"/start", "/help", "help", "❓ Помощь"} else None
                    if bot.api:
                        await bot.api.send_message_async(chat_id, reply, reply_markup=markup)
                except Exception as e:
                    logger.exception("Handle message failed")
                    print(f"[BOT] ERROR: {e!s}")
                    try:
                        if bot.api:
                            await bot.api.send_message_async(chat_id, f"Ошибка: {e!s}")
                    except Exception:  # noqa: BLE001, S110
                        pass
        except httpx.ConnectError as e:
            consecutive_network_errors += 1
            delay = _compute_backoff_delay(consecutive_network_errors)
            logger.warning(
                "Network unavailable (attempt %d). Retrying in %ds. Error: %s",
                consecutive_network_errors, delay, str(e)
            )
            print(f"[BOT] Network down (attempt {consecutive_network_errors}), retry in {delay}s")

            if consecutive_network_errors == MAX_NETWORK_ERRORS_BEFORE_ALERT:
                target_chat = admin_chat_id or (next(iter(config.allowed_chat_ids), None) if config.allowed_chat_ids else None)
                if target_chat and bot.api:
                    try:
                        await bot.api.send_message_async(
                            target_chat,
                            f"⚠️ Сеть недоступна ~{delay}s. Проверь VPN/интернет.",
                            reply_markup=REPLY_KEYBOARD,
                        )
                    except Exception:  # noqa: BLE001, S110
                        pass

            await asyncio.sleep(delay)
            continue

        except httpx.HTTPStatusError as e:
            if e.response is not None and getattr(e.response, "status_code", None) == 409:
                logger.warning("409 Conflict — another bot instance polling. Waiting 60s.")
                print("[BOT] 409 Conflict — waiting 60s")
                await asyncio.sleep(60)
                continue
            logger.exception("HTTP error in polling")
            print(f"[BOT] HTTP error: {e!s}")
            await asyncio.sleep(5)
            continue

        except KeyboardInterrupt:
            scheduler.stop()
            print("\n[BOT] Stopped by user.")
            break
        except Exception as e:
            logger.exception("Polling iteration failed")
            print(f"[BOT] Polling error: {e!s}")
            await asyncio.sleep(2)
            continue


def run_polling(config: TelegramConfig) -> None:
    from jarvis.finance.telegram.review_runtime import consumer_lock
    with consumer_lock(config.token):
        asyncio.run(_run_polling_async(config))


def money(raw: str | Decimal) -> Decimal:
    return Decimal(str(raw).replace(",", ".")).quantize(Decimal("0.01"))


def load_rules(db: FinanceDB) -> dict[str, Any]:
    from jarvis.finance.rules import DEFAULT_RULES

    rules = db.get_setting("rules", None)
    if rules is None or not isinstance(rules, dict):
        db.set_setting("rules", DEFAULT_RULES)
        return DEFAULT_RULES
    return rules


def chunk_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len and current:
            chunks.append(current)
            current = ""
        while len(line) > max_len:
            chunks.append(line[:max_len])
            line = line[max_len:]
        current += line
    if current:
        chunks.append(current)
    return chunks
