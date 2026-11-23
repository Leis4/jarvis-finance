"""Self-learning SMS parser: regex-first, LLM-fallback."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from jarvis.core.llm.orchestrator import complete
from jarvis.core.llm.types import TaskType

logger = logging.getLogger(__name__)


class SMSParseSchema(BaseModel):
    account: str = Field(default="", description="Последние 4 цифры карты/счёта, например 3295")
    time_str: str = Field(default="", description="Время операции HH:MM")
    op_type: str = Field(default="Оплата", description="Оплата | Перевод | Зачисление | Пополнение | Снятие | Возврат")
    amount: float = Field(default=0.0, description="Сумма операции числом")
    currency: str = Field(default="р", description="Валюта")
    merchant: str = Field(default="", description="Магазин/получатель/отправитель")
    balance: float | None = Field(default=None, description="Остаток на балансе после операции, если есть")
    direction: str = Field(default="expense", description="expense | income | transfer")
    regex: str | None = Field(default=None, description="Именованное регулярное выражение с группами account, time, op_type, amount, currency, merchant, balance")


@dataclass
class ParsedSMS:
    account: str            # "3295"
    time_str: str           # "16:49"
    op_type: str            # "Оплата" | "Перевод" | "Зачисление" | "Снятие"
    amount: float           # 10.0
    currency: str           # "р"
    merchant: str           # "Альфа-банк" | "OZON"
    balance: float | None   # 13305.4
    direction: str          # "expense" | "income" | "transfer"
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __await__(self) -> Any:
        async def _coro() -> ParsedSMS:
            return self
        return _coro().__await__()


def _parse_amount(s: Any) -> float | None:
    """Parse '13 305.4' → 13305.4."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s:
        return None
    try:
        clean = s.replace(" ", "").replace("\xa0", "").replace(",", ".")
        return float(clean)
    except (ValueError, TypeError):
        return None


def classify_direction(op_type: str) -> str:
    """Classify: income / expense / transfer."""
    t = (op_type or "").lower()
    if "зачисление" in t or "пополнение" in t or "возврат" in t:
        return "income"
    if "перевод" in t:
        return "transfer"
    return "expense"


DEFAULT_REGEX_PATTERNS = [
    (
        r"СЧЁТ(?P<account>\d{4})\s+"
        r"(?:(?P<time>\d{2}:\d{2})\s+)?"
        r"(?P<op_type>Опл[а-я]+|Покупка|Списание|Перевод|Зачисление|Пополнение|Снятие|Возврат)\s+"
        r"(?P<amount>[\d\s.,]+)(?P<currency>р|руб\.?|₽)\s+"
        r"(?P<merchant>.+?)\s+"
        r"Баланс:?\s*(?P<balance>[\d\s.,]+)(?:р|руб\.?|₽)?"
    ),
    (
        r"СЧЁТ(?P<account>\d{4})\s+(?P<time>\d{2}:\d{2})\s+"
        r"(?P<op_type>Оплата|Перевод|Зачисление|Снятие)\s+"
        r"(?P<amount>[\d\s.,]+)р\s+(?P<merchant>.+?)\s+Баланс:?\s*(?P<balance>[\d\s.,]+)р"
    ),
]


def try_regex_parse(text: str, db_path: str | None = None) -> ParsedSMS | None:
    """Try existing regex patterns from DB, then fallback to built-in default patterns."""
    patterns: list[tuple[int | None, str]] = []
    if db_path:
        try:
            conn = sqlite3.connect(db_path)
            db_patterns = conn.execute("""
                SELECT id, regex_pattern FROM sms_patterns
                ORDER BY match_count DESC, id ASC
            """).fetchall()
            conn.close()
            patterns.extend(db_patterns)
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass

    for def_pat in DEFAULT_REGEX_PATTERNS:
        if not any(p[1] == def_pat for p in patterns):
            patterns.append((None, def_pat))

    for pat_id, regex in patterns:
        try:
            m = re.search(regex, text, re.DOTALL | re.IGNORECASE)
            if not m:
                continue
            g = m.groupdict()
            if not g:
                continue

            result = ParsedSMS(
                account=str(g.get("account", "")),
                time_str=str(g.get("time", "") or g.get("time_str", "")),
                op_type=str(g.get("op_type", "")),
                amount=_parse_amount(g.get("amount", "")) or 0.0,
                currency=str(g.get("currency", "р") or "р"),
                merchant=(g.get("merchant", "") or "").strip(),
                balance=_parse_amount(g.get("balance", "")),
                direction="",
                raw_text=text,
            )
            result.direction = classify_direction(result.op_type)

            if pat_id is not None and db_path:
                try:
                    conn = sqlite3.connect(db_path)
                    conn.execute("""
                        UPDATE sms_patterns SET match_count = match_count + 1,
                            last_used_at = ? WHERE id = ?
                    """, (datetime.now(UTC).isoformat(), pat_id))
                    conn.commit()
                    conn.close()
                except Exception:  # noqa: BLE001, S110
                    pass

            return result
        except re.error:
            continue
        except Exception:
            logger.exception(f"Regex {pat_id} failed")
            continue

    return None


async def llm_parse(text: str, db_path: str) -> ParsedSMS | None:
    """Parse via core LLM orchestrator. Also generates regex pattern."""
    import asyncio

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001, S110
        pass

    prompt = (
        "Извлеки структурированные поля из SMS Сбербанка.\n"
        f"Текст SMS: {text}\n"
    )

    for attempt in range(2):
        try:
            llm_res = await asyncio.wait_for(
                complete(
                    task=TaskType.CATEGORIZE_TX,
                    prompt=prompt,
                    response_schema=SMSParseSchema,
                ),
                timeout=60.0,
            )
            if not llm_res.parsed or not isinstance(llm_res.parsed, SMSParseSchema):
                continue

            data = llm_res.parsed
            balance_val = _parse_amount(data.balance)
            result = ParsedSMS(
                account=str(data.account or ""),
                time_str=str(data.time_str or ""),
                op_type=str(data.op_type or "Оплата"),
                amount=_parse_amount(data.amount) or 0.0,
                currency=str(data.currency or "р"),
                merchant=str(data.merchant or ""),
                balance=balance_val,
                direction=str(data.direction or "expense"),
                raw_text=text,
            )

            # Save regex pattern
            regex = data.regex
            if not regex or not re.search(regex, text, re.DOTALL):
                regex = (
                    r"СЧЁТ(?P<account>\d{4})\s+(?P<time>\d{2}:\d{2})\s+"
                    rf"(?P<op_type>{re.escape(result.op_type)}|Оплата|Перевод|Зачисление|Снятие)\s+"
                    rf"(?P<amount>[\d\s.,]+){re.escape(result.currency)}\s+(?P<merchant>.+?)\s+"
                    rf"Баланс:?\s*(?P<balance>[\d\s.,]+){re.escape(result.currency)}"
                )

            if regex and db_path:
                try:
                    if re.search(regex, text, re.DOTALL):
                        conn = sqlite3.connect(db_path)
                        conn.execute("""
                            CREATE TABLE IF NOT EXISTS sms_patterns (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                regex_pattern TEXT NOT NULL,
                                example_text TEXT,
                                parsed_fields TEXT,
                                match_count INTEGER DEFAULT 0,
                                created_at TEXT NOT NULL,
                                last_used_at TEXT
                            )
                        """)
                        conn.execute("""
                            INSERT INTO sms_patterns
                            (regex_pattern, example_text, parsed_fields,
                             match_count, created_at, last_used_at)
                            VALUES (?, ?, ?, 0, ?, ?)
                        """, (
                            regex, text[:500],
                            json.dumps(list(result.to_dict().keys())),
                            datetime.now(UTC).isoformat(),
                            datetime.now(UTC).isoformat(),
                        ))
                        conn.commit()
                        conn.close()
                        logger.info(f"New pattern saved: {regex[:60]}...")
                except Exception:
                    logger.exception("Failed to save pattern")

            return result
        except Exception:
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            logger.exception("LLM parse failed")
            return None
    return None


class _LLMFallbackAwaitable:
    def __init__(self, text: str, db_path: str):
        self.text = text
        self.db_path = db_path

    def __await__(self) -> Any:
        return llm_parse(self.text, self.db_path).__await__()

    def _sync(self) -> ParsedSMS | None:
        import asyncio
        return asyncio.run(llm_parse(self.text, self.db_path))

    def __getitem__(self, key: str) -> Any:
        res = self._sync()
        if res is None:
            raise KeyError(key)
        return res[key]

    def to_dict(self) -> dict[str, Any]:
        res = self._sync()
        return res.to_dict() if res else {}


def parse_sms(text: str, db_path: str | None = None) -> Any:
    """Main: regex first, LLM fallback."""
    if db_path is None:
        import os
        db_path = os.getenv("FINANCE_AGENT_DB", "datasets/student_finance.db")
    result = try_regex_parse(text, db_path)
    if result:
        logger.debug("Parsed via regex")
        return result
    logger.info("No regex matched — using LLM")
    return _LLMFallbackAwaitable(text, db_path)


def parse_sms_sync(text: str, db_path: str | None = None) -> ParsedSMS | None:
    """Sync wrapper."""
    if db_path is None:
        import os
        db_path = os.getenv("FINANCE_AGENT_DB", "datasets/student_finance.db")
    res = parse_sms(text, db_path)
    if isinstance(res, ParsedSMS):
        return res
    if isinstance(res, _LLMFallbackAwaitable):
        return res._sync()
    if res is None:
        return None
    import asyncio
    parsed: ParsedSMS | None = asyncio.run(res)
    return parsed
