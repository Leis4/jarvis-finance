from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.review_store import ReviewStore
from jarvis.finance.telegram.bot import TelegramAPI

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Wrapper for sending notifications via TelegramAPI."""

    def __init__(self, bot: TelegramAPI | Any | None = None, chat_id: int | str | None = None) -> None:
        if bot is None:
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            if token:
                try:
                    bot = TelegramAPI(token)
                except Exception:  # noqa: BLE001
                    logger.warning("Telegram transport initialization unavailable; private diagnostics withheld")
                    bot = None

        if chat_id is None:
            chat_id_env = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()
            if chat_id_env:
                try:
                    chat_id = int(chat_id_env)
                except ValueError:
                    chat_id = chat_id_env

        self._bot = bot
        self._chat_id = chat_id

    def is_configured(self) -> bool:
        return self._bot is not None and self._chat_id is not None

    def send(self, text: str, parse_mode: str | None = None) -> bool:
        """Send message via bot. Returns True on success."""
        if not self.is_configured():
            logger.debug("Notifier not configured, skipping delivery")
            return False

        try:
            target_chat_id = int(self._chat_id) if isinstance(self._chat_id, (int, str)) and str(self._chat_id).isdigit() else self._chat_id
            payload: dict[str, Any] = {"chat_id": target_chat_id, "text": text}
            if parse_mode:
                payload["parse_mode"] = parse_mode

            bot = self._bot
            if bot is not None and hasattr(bot, "request"):
                res = bot.request("sendMessage", payload)
                return bool(res.get("ok"))
            if bot is not None and hasattr(bot, "send_message"):
                if isinstance(target_chat_id, int):
                    bot.send_message(chat_id=target_chat_id, text=text)
                else:
                    bot.send_message(chat_id=int(target_chat_id or 0), text=text)
                return True
            return False
        except Exception:  # noqa: BLE001 - fail closed at the transport boundary
            logger.warning("Telegram delivery failed; private diagnostics withheld")
            return False

    def send_safe(self, text: str) -> bool:
        """Send plain text once; a timeout cannot prove nondelivery."""
        return self.send(text)


def claim_delivery(db: FinanceDB | ReviewStore, delivery_key: str, text: str) -> str:
    """Return claimed for a new attempt or the persistent previous state."""
    with db.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS notification_deliveries (
            delivery_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL)""")
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT status FROM notification_deliveries WHERE delivery_key = ?", (delivery_key,)).fetchone()
        if previous:
            return str(previous["status"])
        scope = delivery_key.partition(":")[0]
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        ambiguous = conn.execute("""SELECT status FROM notification_deliveries
            WHERE delivery_key LIKE ? AND content_hash=? AND status IN ('started','unknown') LIMIT 1""",
            (scope + ":%", content_hash)).fetchone()
        if ambiguous:
            return str(ambiguous["status"])
        conn.execute("INSERT INTO notification_deliveries VALUES (?, ?, 'started', ?)",
                     (delivery_key, content_hash, datetime.now(UTC).isoformat()))
        conn.commit()
    return "claimed"


def finish_delivery(db: FinanceDB | ReviewStore, delivery_key: str, sent: bool) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE notification_deliveries SET status = ? WHERE delivery_key = ?",
                     ("delivered" if sent else "unknown", delivery_key))
        conn.commit()


def send_once(db: FinanceDB, notifier: TelegramNotifier, delivery_key: str, text: str) -> bool:
    """Claim before sending; scope the journal to the destination."""
    if not notifier.is_configured():
        return False
    destination = hashlib.sha256(str(getattr(notifier, "_chat_id", "default")).encode()).hexdigest()[:16]
    delivery_key = f"{destination}:{delivery_key}"
    previous = claim_delivery(db, delivery_key, text)
    if previous != "claimed":
        return previous == "delivered"
    try:
        sent = bool(notifier.send(text))
    except Exception:  # noqa: BLE001
        sent = False
    finish_delivery(db, delivery_key, sent)
    return sent
