"""Dispatch notifications with quiet-hours and cooldown."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta

from jarvis.finance.notifications.models import NotificationEvent, Severity
from jarvis.finance.notifications.triggers import run_all_checks

logger = logging.getLogger(__name__)

QUIET_START = 23
QUIET_END = 7


def _is_quiet_hours() -> bool:
    h = datetime.now(UTC).hour
    return h >= QUIET_START or h < QUIET_END


def _ensure_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                dedup_key TEXT,
                title TEXT,
                message TEXT,
                sent_at TEXT NOT NULL,
                chat_id TEXT NOT NULL DEFAULT ''
            )
        """)
        if "chat_id" not in {row[1] for row in conn.execute("PRAGMA table_info(notifications_log)")}:
            conn.execute("ALTER TABLE notifications_log ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


def _was_recently_sent(db_path: str, dedup_key: str, cooldown_min: int = 60, chat_id: str | None = None) -> bool:
    if not dedup_key:
        return False
    _ensure_table(db_path)
    cutoff = (datetime.now(UTC) - timedelta(minutes=cooldown_min)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("""
            SELECT 1 FROM notifications_log
            WHERE dedup_key = ? AND sent_at >= ? AND (? IS NULL OR chat_id=?) LIMIT 1
        """, (dedup_key, cutoff, chat_id, chat_id)).fetchone()
        return bool(row)
    finally:
        conn.close()


def _log_sent(db_path: str, event: NotificationEvent, chat_id: str = "") -> None:
    _ensure_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            INSERT INTO notifications_log
            (event_type, severity, dedup_key, title, message, sent_at, chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            event.event_type.value, event.severity.value, event.dedup_key,
            event.title, event.message, datetime.now(UTC).isoformat(), chat_id,
        ))
        conn.commit()
    finally:
        conn.close()


async def dispatch_notifications(db_path: str, chat_id: int | None = None) -> int:
    """Check all triggers, apply quiet-hours + cooldown, send via Telegram."""
    from jarvis.finance.db import FinanceDB
    from jarvis.finance.scheduler.telegram_notify import claim_delivery, finish_delivery
    from jarvis.finance.telegram.bot import TelegramAPI

    if chat_id is None:
        chat_id_env = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
        if not chat_id_env:
            return 0
        chat_id = int(chat_id_env)

    events = run_all_checks(db_path)
    if not events:
        return 0

    quiet = _is_quiet_hours()
    to_send = []
    for e in events:
        if quiet and e.severity != Severity.CRITICAL:
            continue
        if _was_recently_sent(db_path, e.dedup_key, chat_id=str(chat_id)):
            continue
        to_send.append(e)

    if not to_send:
        return 0

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return 0

    api = TelegramAPI(token)
    sent = 0
    for e in to_send:
        text = f"{e.title}\n\n{e.message}"
        digest = hashlib.sha256(f"{chat_id}:{e.dedup_key}:{text}".encode()).hexdigest()
        # The hourly scheduler retry reuses the same generation. Delivered events
        # additionally retain the existing rolling cooldown check.
        generation = datetime.now(UTC).strftime("%Y-%m-%dT%H")
        destination = hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]
        key = f"{destination}:smart:{generation}:{digest}"
        db = FinanceDB(db_path)
        if claim_delivery(db, key, text) != "claimed":
            continue
        delivered = False
        try:
            await api.send_message_async(int(chat_id), text)
            delivered = True
            _log_sent(db_path, e, str(chat_id))
            sent += 1
        except Exception:
            logger.exception("Send failed for %s", e.event_type)
        finally:
            finish_delivery(db, key, delivered)
    return sent


def dispatch_sync(db_path: str, chat_id: int | None = None) -> int:
    """Synchronous wrapper around dispatch_notifications."""
    return asyncio.run(dispatch_notifications(db_path, chat_id))
