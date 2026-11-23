"""Ingest parsed SMS into transactions."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from jarvis.finance.sms.parser import ParsedSMS

logger = logging.getLogger(__name__)


def ingest_sms(parsed: ParsedSMS, db_path: str) -> dict[str, Any]:
    """Insert parsed SMS as transaction."""
    if parsed.amount <= 0:
        return {"inserted": False, "reason": "zero_amount"}

    amount = -parsed.amount if parsed.direction == "expense" else parsed.amount
    desc = f"{parsed.op_type} {parsed.merchant}".strip()

    conn = sqlite3.connect(db_path)
    try:
        tx_date = datetime.now(UTC).date().isoformat()
        now = datetime.now(UTC).isoformat()

        # Dedup: same amount + same merchant today
        existing = conn.execute("""
            SELECT id FROM transactions
            WHERE ABS(ABS(amount) - ?) < 0.01
              AND description LIKE ?
              AND tx_date = ?
            LIMIT 1
        """, (parsed.amount, f"%{parsed.merchant}%", tx_date)).fetchone()

        if existing:
            return {"inserted": False, "reason": "duplicate", "tx_id": existing[0]}


        cursor = conn.execute("""
            INSERT INTO transactions
            (tx_date, amount, description, category, created_at)
            VALUES (?, ?, ?, 'uncategorized', ?)
        """, (tx_date, str(amount), desc, now))

        tx_id = cursor.lastrowid
        conn.commit()

        return {"inserted": True, "tx_id": tx_id}
    except Exception as e:
        conn.rollback()
        logger.exception("Ingest failed")
        return {"inserted": False, "reason": str(e)}
    finally:
        conn.close()


def ingest_sms_batch(sms_list: list[str], db_path: str) -> dict[str, Any]:
    """Process multiple SMS."""
    from jarvis.finance.sms.parser import parse_sms_sync
    inserted = skipped = errors = 0
    for text in sms_list:
        try:
            parsed = parse_sms_sync(text, db_path)
            if not parsed:
                errors += 1
                continue
            result = ingest_sms(parsed, db_path)
            if result.get("inserted"):
                inserted += 1
            else:
                skipped += 1
        except Exception:  # noqa: BLE001
            errors += 1
    return {"inserted": inserted, "skipped": skipped, "errors": errors}
