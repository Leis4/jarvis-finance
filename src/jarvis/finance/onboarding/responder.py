"""Apply user's answer + learn. Also handle rejections."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from jarvis.finance.classification.taxonomy import INCOMING_CLASSES, OUTGOING_CLASSES


def apply_answer(db_path: str, qid: int, answer_value: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    now = datetime.now(UTC).isoformat()

    row = conn.execute("""
        SELECT question_type, direction, target_id, amount, description
        FROM question_queue WHERE id = ?
    """, (qid,)).fetchone()
    if not row:
        conn.close()
        return {"applied": False, "reason": "not_found"}

    _qtype, direction, target_id, amount, desc = row

    if answer_value == "skip":
        conn.execute("UPDATE question_queue SET status='skipped' WHERE id=?", (qid,))
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(1) FROM question_queue WHERE status='pending'"
        ).fetchone()[0]
        conn.close()
        return {"applied": False, "reason": "skipped", "remaining": remaining}

    parts = answer_value.split(":")
    top_class = parts[0]
    subtype = parts[1] if len(parts) > 1 else None
    amount_f = float(amount or 0)

    if direction == "incoming":
        if top_class in INCOMING_CLASSES:
            internal_type = INCOMING_CLASSES[top_class]["aliases"][0]
        else:
            internal_type = top_class

        conn.execute("""
            INSERT INTO incoming_classifications (income_tx_id, transfer_type, status, confidence, reasoning, created_at, confirmed_at)
            VALUES (?, ?, 'confirmed', 1.0, 'user_answer', ?, ?)
            ON CONFLICT(income_tx_id) DO UPDATE SET
                transfer_type = excluded.transfer_type,
                status = 'confirmed',
                confidence = 1.0,
                confirmed_at = excluded.confirmed_at
        """, (target_id, internal_type, now, now))
        _learn_incoming(conn, desc or "", amount_f, internal_type, subtype, now)
        main_type = internal_type
    else:
        if top_class in OUTGOING_CLASSES:
            internal_category = _map_outgoing(top_class, subtype)
        else:
            internal_category = top_class

        conn.execute(
            "UPDATE transactions SET category = ? WHERE id = ?",
            (internal_category, target_id),
        )
        _learn_outgoing(conn, desc or "", abs(amount_f), internal_category, subtype, now)
        main_type = internal_category

    conn.execute("""
        UPDATE question_queue
        SET status = 'answered', user_answer = ?, answered_at = ?
        WHERE id = ?
    """, (answer_value, now, qid))

    remaining = conn.execute(
        "SELECT COUNT(1) FROM question_queue WHERE status='pending'"
    ).fetchone()[0]
    conn.commit()
    conn.close()
    return {
        "applied": True,
        "type": main_type,
        "subtype": subtype,
        "remaining": remaining,
    }


def _map_outgoing(top_class: str, subtype: str | None) -> str:
    """Map top-class + subtype to internal category."""
    if top_class == "entertainment":
        if subtype == "vape":
            return "alcohol_tobacco"  # legacy
        if subtype == "games":
            return "games_digital"
        return "entertainment"
    if top_class == "health":
        if subtype == "medication":
            return "health"
        if subtype in ("sport", "fitness"):
            return "sport"
        return "health"
    if top_class == "bills":
        if subtype == "subscriptions":
            return "subscriptions"
        return "bills"
    if top_class == "food":
        if subtype in ("cafe", "restaurant"):
            return "cafes"
        if subtype == "delivery":
            return "delivery"
        return "food_basic"
    if top_class == "transport":
        if subtype == "taxi":
            return "taxi"
        return "transport"
    if top_class == "shopping":
        if subtype == "marketplace":
            return "shopping_marketplace"
        return "shopping"
    if top_class == "housing":
        return "housing_utilities"
    if top_class == "transfer_out":
        return "peer_transfer_out"
    return top_class


def _learn_incoming(conn: sqlite3.Connection, desc: str, amount: float, ttype: str, subtype: str | None, now: str) -> None:
    bank = _extract_bank(desc)
    amin, amax = amount * 0.95, amount * 1.05
    label = subtype or ttype

    existing = conn.execute("""
        SELECT id FROM known_senders
        WHERE (bank_hint = ? OR bank_hint IS NULL) AND transfer_type = ?
          AND amount_min IS NOT NULL
          AND ? BETWEEN amount_min * 0.8 AND amount_max * 1.2
        LIMIT 1
    """, (bank, ttype, amount)).fetchone()

    if existing:
        conn.execute("""
            UPDATE known_senders
            SET times_confirmed = times_confirmed + 1,
                amount_min = MIN(COALESCE(amount_min, ?), ?),
                amount_max = MAX(COALESCE(amount_max, ?), ?),
                last_seen_at = ?
            WHERE id = ?
        """, (amin, amin, amax, amax, now, existing[0]))
    else:
        conn.execute("""
            INSERT INTO known_senders
            (sender_pattern, transfer_type, bank_hint, amount_min, amount_max,
             sender_label, context_description, times_confirmed,
             auto_classify_after, direction, confidence, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 5, 'incoming', 0.7, ?, ?)
        """, (
            f"{bank}:{label}:{amount:.0f}",
            ttype, bank, amin, amax, label, desc[:200], now, now,
        ))


def _learn_outgoing(conn: sqlite3.Connection, desc: str, amount: float, cat: str, subtype: str | None, now: str) -> None:
    merchant = _extract_merchant(desc)
    amin, amax = amount * 0.9, amount * 1.1
    label = subtype or cat

    existing = conn.execute("""
        SELECT id FROM outgoing_patterns
        WHERE merchant_pattern = ? AND typical_category = ?
          AND amount_min IS NOT NULL
          AND ? BETWEEN amount_min * 0.8 AND amount_max * 1.2
        LIMIT 1
    """, (merchant, cat, amount)).fetchone()

    if existing:
        conn.execute("""
            UPDATE outgoing_patterns
            SET times_confirmed = times_confirmed + 1,
                amount_min = MIN(COALESCE(amount_min, ?), ?),
                amount_max = MAX(COALESCE(amount_max, ?), ?),
                last_seen_at = ?
            WHERE id = ?
        """, (amin, amin, amax, amax, now, existing[0]))
    else:
        conn.execute("""
            INSERT INTO outgoing_patterns
            (merchant_pattern, amount_min, amount_max, typical_category,
             user_label, times_confirmed, auto_classify_after,
             created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, 1, 5, ?, ?)
        """, (merchant, amin, amax, cat, label, now, now))


def _extract_bank(desc: str) -> str:
    d = (desc or "").lower()
    for b in ["t-bank", "tbank", "sbol", "сбер", "sber", "альфа", "alfa",
              "vtb", "втб", "yandex", "вузов", "универ"]:
        if b in d:
            return b
    return "unknown"


def _extract_merchant(desc: str) -> str:
    d = (desc or "").strip()
    d = re.sub(r"^(операция по (карте|счету|счёту)\s+\*+\d+\s*)", "", d, flags=re.IGNORECASE)
    return " ".join(d.split()[:3]).lower()[:50]
