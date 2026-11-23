"""Build question queue from escalations + unclassified."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from jarvis.finance.classification.taxonomy import get_ui_classes
from jarvis.finance.incoming.classifier import is_non_incoming


def _create_question_for_escalation(db_path: str, tx_id: int, result: dict[str, Any]) -> None:
    """Create question when classifier escalated."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT amount, description FROM transactions WHERE id = ?", (tx_id,)
    ).fetchone()
    if not row:
        conn.close()
        return
    amount, desc = row

    if is_non_incoming(desc or ""):
        now = datetime.now(UTC).isoformat()
        conn.execute("""
            INSERT INTO incoming_classifications
            (income_tx_id, transfer_type, confidence, status, reasoning, created_at)
            VALUES (?, 'internal_transfer', 1.0, 'auto_matched', 'self_transfer_excluded', ?)
            ON CONFLICT(income_tx_id) DO UPDATE SET
                transfer_type = 'internal_transfer',
                status = 'auto_matched',
                confidence = 1.0
        """, (tx_id, now))
        conn.commit()
        conn.close()
        return

    exists = conn.execute("""
        SELECT 1 FROM question_queue
        WHERE target_id = ? AND status = 'pending'
    """, (tx_id,)).fetchone()
    if exists:
        conn.close()
        return

    amt_val = float(amount or 0)
    direction = "incoming" if amt_val > 0 else "outgoing"
    suggestions = _default_suggestions(desc or "", amt_val, direction)
    conn.execute("""
        INSERT INTO question_queue
        (question_type, direction, target_id, amount, description,
         question_text, suggested_answers, priority, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 2, ?)
    """, (
        f"{direction}_unclassified",
        direction,
        tx_id, amt_val, desc or "",
        f"💰 {abs(amt_val):.0f}₽\n📝 {(desc or '')[:80]}",
        json.dumps(suggestions, ensure_ascii=False),
        datetime.now(UTC).isoformat(),
    ))
    conn.commit()
    conn.close()


def _default_suggestions(desc: str, amount: float, direction: str | None = None) -> list[dict[str, Any]]:
    if direction is None:
        direction = "incoming" if float(amount or 0) > 0 else "outgoing"
    return get_ui_classes(direction)


def get_next_question(db_path: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(db_path)
    row = conn.execute("""
        SELECT id, question_type, direction, target_id, amount,
               question_text, suggested_answers
        FROM question_queue WHERE status = 'pending'
        ORDER BY priority ASC, created_at ASC LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        return None
    try:
        suggs = json.loads(row[6])
    except Exception:  # noqa: BLE001
        suggs = []
    return {
        "id": row[0],
        "question_type": row[1],
        "direction": row[2],
        "target_id": row[3],
        "amount": row[4],
        "text": row[5],
        "suggestions": suggs,
    }


def mark_answered(db_path: str, qid: int, answer: str) -> None:
    conn = sqlite3.connect(db_path)
    status_str = "skipped" if answer == "skip" else "answered"
    conn.execute("""
        UPDATE question_queue
        SET status = ?, user_answer = ?, answered_at = ?
        WHERE id = ?
    """, (
        status_str,
        answer,
        datetime.now(UTC).isoformat(),
        qid,
    ))
    conn.commit()
    conn.close()


def count_pending(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(1) FROM question_queue WHERE status = 'pending'"
    ).fetchone()[0]
    conn.close()
    return int(n)


def build_queue_from_unclassified(db_path: str) -> int:
    """Build question queue from unclassified incoming and outgoing transactions."""
    conn = sqlite3.connect(db_path)
    now_str = datetime.now(UTC).isoformat()
    count_added = 0

    # 1. Unclassified incoming
    incoming_rows = conn.execute("""
        SELECT ic.income_tx_id, t.amount, t.description, ic.confidence, ic.transfer_type
        FROM incoming_classifications ic
        JOIN transactions t ON ic.income_tx_id = t.id
        WHERE ic.status = 'pending' AND (ic.confidence < 0.85 OR ic.transfer_type = 'unknown')
    """).fetchall()

    for tx_id, amt, desc, conf, ttype in incoming_rows:
        if is_non_incoming(desc or ""):
            conn.execute("""
                INSERT INTO incoming_classifications
                (income_tx_id, transfer_type, confidence, status, reasoning, created_at)
                VALUES (?, 'internal_transfer', 1.0, 'auto_matched', 'self_transfer_excluded', ?)
                ON CONFLICT(income_tx_id) DO UPDATE SET
                    transfer_type = 'internal_transfer',
                    status = 'auto_matched',
                    confidence = 1.0
            """, (tx_id, now_str))
            continue

        exists = conn.execute("SELECT 1 FROM question_queue WHERE target_id = ? AND status = 'pending'", (tx_id,)).fetchone()
        if not exists:
            amt_val = float(amt or 0)
            suggestions = _default_suggestions(desc or "", amt_val, "incoming")
            q_text = f"💰 {abs(amt_val):.0f}₽ (Входящий)\n📝 {(desc or '')[:80]}\n🤔 Тип: {ttype} (уверенность {conf:.0%})"
            conn.execute("""
                INSERT INTO question_queue
                (question_type, direction, target_id, amount, description,
                 question_text, suggested_answers, priority, created_at)
                VALUES ('incoming_unclassified', 'incoming', ?, ?, ?, ?, ?, 1, ?)
            """, (tx_id, amt_val, desc or "", q_text, json.dumps(suggestions, ensure_ascii=False), now_str))
            count_added += 1

    # 2. Unclassified outgoing
    outgoing_rows = conn.execute("""
        SELECT id, amount, description, category
        FROM transactions
        WHERE (category IN ('uncategorized', 'unknown', 'other', '') OR category IS NULL)
          AND CAST(amount AS REAL) < 0
        LIMIT 100
    """).fetchall()

    for tx_id, amt, desc, cat in outgoing_rows:
        exists = conn.execute("SELECT 1 FROM question_queue WHERE target_id = ? AND status = 'pending'", (tx_id,)).fetchone()
        if not exists:
            amt_val = float(amt or 0)
            suggestions = _default_suggestions(desc or "", amt_val, "outgoing")
            q_text = f"💳 {abs(amt_val):.0f}₽ (Расход)\n📝 {(desc or '')[:80]}\n🤔 Категория: {cat or 'не определена'}"
            conn.execute("""
                INSERT INTO question_queue
                (question_type, direction, target_id, amount, description,
                 question_text, suggested_answers, priority, created_at)
                VALUES ('outgoing_unclassified', 'outgoing', ?, ?, ?, ?, ?, 3, ?)
            """, (tx_id, amt_val, desc or "", q_text, json.dumps(suggestions, ensure_ascii=False), now_str))
            count_added += 1

    conn.commit()
    conn.close()
    return count_added


def clean_self_transfers_from_queue(db_path: str) -> int:
    """Remove self-transfers from queue, mark as internal_transfer."""
    conn = sqlite3.connect(db_path)

    # 1. Все pending classifications с self-patterns
    rows = conn.execute("""
        SELECT ic.income_tx_id, t.description
        FROM incoming_classifications ic
        JOIN transactions t ON ic.income_tx_id = t.id
        WHERE ic.status = 'pending'
    """).fetchall()

    to_fix = [tid for tid, desc in rows if is_non_incoming(desc or "")]

    if to_fix:
        placeholders = ",".join("?" * len(to_fix))
        # Update classifications
        conn.execute(f"""
            UPDATE incoming_classifications
            SET transfer_type = 'internal_transfer',
                status = 'auto_matched',
                confidence = 1.0,
                reasoning = 'self_transfer_excluded'
            WHERE income_tx_id IN ({placeholders})
        """, to_fix)
        # Delete from question queue
        conn.execute(f"""
            DELETE FROM question_queue
            WHERE target_id IN ({placeholders})
        """, to_fix)

    conn.commit()
    n = len(to_fix)
    conn.close()
    return n
