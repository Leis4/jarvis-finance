"""Main classifier: combines rules + LLM."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from jarvis.finance.classification.layers import l0_preprocess, l1_hard_rules
from jarvis.finance.classification.orchestrator import classify_transaction
from jarvis.finance.classification.taxonomy import normalize_category

PARENTS = [
    "николай николаевич", "ольга николаевна",
    "николай смирнов", "смирнов николай", "ольга смирнова", "смирнова ольга"
]

GIFT_KEYWORDS = ["с др", "день рождения", "днём рождения", "подарок", 
                 "новый год", "рождество", "поздравляю"]


def _parse_typical_amounts(raw: Any) -> list[float]:
    """Parse typical_amounts from any format: JSON, CSV string, list."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    if isinstance(raw, (int, float)):
        return [float(raw)]
    if isinstance(raw, str):
        raw = raw.strip()
        # Try JSON first
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [float(x) for x in parsed]
            return [float(parsed)]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        # Fallback: comma-separated
        try:
            return [float(x.strip()) for x in raw.split(",") if x.strip()]
        except (ValueError, TypeError):
            return []
    return []


def _amount_matches(amount: float, typical: list[float], tolerance: float = 0.05) -> bool:
    """Check if amount matches any typical with ±tolerance."""
    if not typical:
        return False
    for t in typical:
        if t <= 0:
            continue
        if abs(amount - t) / t <= tolerance:
            return True
    return False


def _bank_matches(desc: str, bank_hint: str) -> bool:
    """Check if description matches bank_hint (case-insensitive, variant-aware)."""
    if not bank_hint or not desc:
        return False
    desc_low = desc.lower()
    bank_low = bank_hint.lower().strip()
    # Normalize variants
    if bank_low in ("t-bank", "tbank", "тинёк", "тинек", "тинькофф", "tinkoff"):
        return "t-bank" in desc_low or "tbank" in desc_low or "т-банк" in desc_low or "тбанк" in desc_low or "тинек" in desc_low or "тинёк" in desc_low or "tinkoff" in desc_low or "тинькофф" in desc_low
    if bank_low in ("sbol", "сбер", "sberbank", "сбербанк"):
        return "sbol" in desc_low or "сбер" in desc_low or "sber" in desc_low or "сбербанк" in desc_low
    return bank_low in desc_low


def _match_parent_pattern(db_path: str, desc: str, amount: float) -> dict[str, Any] | None:
    """Match parental pattern by bank + amount (case-insensitive)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT transfer_type, typical_category, bank_hint, typical_amounts
        FROM known_senders
        WHERE transfer_type = 'parental_support'
    """).fetchall()
    conn.close()

    for ttype, cat, bank_hint, amounts_raw in rows:
        if bank_hint and not _bank_matches(desc, bank_hint):
            continue
        typical = _parse_typical_amounts(amounts_raw)
        if _amount_matches(amount, typical):
            return {
                "transfer_type": ttype,
                "target_category": cat,
                "confidence": 0.95,
                "reasoning": f"bank={bank_hint} amount={amount:.0f}",
            }
    return None


def _extract_sender(description: str) -> str:
    m = re.search(r"[Оо]т\s+([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)", description or "")
    return m.group(1).strip() if m else ""


_LLM_DISABLED = False


def is_non_incoming(description: str) -> bool:
    """Check if transaction is a self-transfer, ATM withdrawal, or internal operation."""
    desc_low = (description or "").lower()
    SELF_PATTERNS = [
        "пётр николайович",
        "смирнов пётр",
        "г. пётр",
        "пётр николай",
        "с. пётр",
        "sultan azat",
        "пётр",
    ]
    if any(p in desc_low for p in SELF_PATTERNS):
        return True
    if "atm" in desc_low:
        return True
    if "vklad" in desc_low:
        return True
    if "брокерск" in desc_low:
        return True
    return "dbo transfer" in desc_low


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
    suggestions = ["parental_support", "gift", "salary", "compensation"]
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


def classify_incoming(db_path: str, income_tx_id: int, use_llm: bool = True) -> dict[str, Any] | None:
    """Classify a single incoming transaction. Returns classification dict."""
    conn = sqlite3.connect(db_path)
    row = conn.execute("""
        SELECT id, amount, description, tx_date, category
        FROM transactions WHERE id = ?
    """, (income_tx_id,)).fetchone()
    conn.close()

    if not row:
        return None

    tx_id, amount, description, _tx_date, category = row
    amount_val = float(amount) if amount is not None else 0.0
    if amount_val <= 0:
        return None

    desc = description or ""
    if is_non_incoming(desc):
        return None

    # Priority 0: Name patterns / hard rules / 16-layer orchestrator
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            try:
                import nest_asyncio  # type: ignore[import-not-found]
                nest_asyncio.apply()
                result = loop.run_until_complete(classify_transaction(db_path, desc, amount_val, category=category))
            except Exception:  # noqa: BLE001
                # If nested loop not allowed, create new loop or run sync layer directly
                ctx = l0_preprocess(desc, amount_val, category=category)
                l1_res = l1_hard_rules(db_path, ctx)
                if l1_res:
                    result = {"category": l1_res.category, "confidence": l1_res.confidence, "reasoning": [f"{l1_res.layer}: {l1_res.reasoning}"], "escalate": False}
                else:
                    result = {"category": "unknown", "confidence": 0.0, "reasoning": ["async_loop_fallback"], "escalate": True}
        else:
            result = asyncio.run(classify_transaction(db_path, desc, amount_val, category=category))
    except Exception as e:  # noqa: BLE001
        result = {"category": "unknown", "confidence": 0.0, "reasoning": [f"orchestrator_error: {e}"], "escalate": True}

    if result.get("escalate"):
        try:
            _create_question_for_escalation(db_path, tx_id, result)
        except Exception:  # noqa: BLE001, S110
            pass

    ui_category = normalize_category(result.get("category", "unknown"))

    return {
        "income_tx_id": tx_id,
        "transfer_type": result.get("category", "unknown"),
        "ui_category": ui_category,
        "matched_expense_tx_id": None,
        "target_category": None,
        "confidence": result.get("confidence", 0.0),
        "reasoning": "; ".join(result.get("reasoning", [])) if isinstance(result.get("reasoning"), list) else str(result.get("reasoning", "")),
    }


def save_classification(db_path: str, classification: dict[str, Any]) -> None:
    """Save classification record into incoming_classifications table."""
    conn = sqlite3.connect(db_path)
    now = datetime.now(UTC).isoformat()
    status = classification.get("status") or ("auto_matched" if classification.get("confidence", 0) >= 0.9 else "pending")
    conn.execute("""
        INSERT INTO incoming_classifications
        (income_tx_id, transfer_type, matched_expense_tx_id, target_category, confidence, status, reasoning, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(income_tx_id) DO UPDATE SET
            transfer_type = excluded.transfer_type,
            matched_expense_tx_id = excluded.matched_expense_tx_id,
            target_category = excluded.target_category,
            confidence = excluded.confidence,
            status = excluded.status,
            reasoning = excluded.reasoning
    """, (
        classification["income_tx_id"],
        classification["transfer_type"],
        classification.get("matched_expense_tx_id"),
        classification.get("target_category"),
        classification.get("confidence", 0.0),
        status,
        classification.get("reasoning", ""),
        now,
    ))
    conn.commit()
    conn.close()


def fix_pending_high_confidence(db_path: str) -> int:
    """One-time: promote pending classifications with conf>=0.9 to auto_matched."""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("""
        UPDATE incoming_classifications
        SET status = 'auto_matched'
        WHERE status = 'pending' AND confidence >= 0.9
    """)
    n = cursor.rowcount
    conn.commit()
    conn.close()
    return n


def reset_parental_classifications(db_path: str) -> int:
    """Reset all parental_support classifications and re-classify from scratch."""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("""
        DELETE FROM incoming_classifications
        WHERE transfer_type = 'parental_support'
    """)
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    process_all_unclassified_incoming(db_path, use_llm=False)
    return deleted


def process_all_unclassified_incoming(db_path: str, use_llm: bool = False, days: int = 365) -> int:
    """Process and classify all incoming transactions without classification record or with unknown classification."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT t.id FROM transactions t
        LEFT JOIN incoming_classifications ic ON t.id = ic.income_tx_id
        WHERE CAST(t.amount AS REAL) > 0 AND (ic.id IS NULL OR ic.transfer_type = 'unknown')
    """).fetchall()
    conn.close()

    count = 0
    for row in rows:
        tx_id = row[0]
        classification = classify_incoming(db_path, tx_id, use_llm=use_llm)
        if classification:
            save_classification(db_path, classification)
            count += 1
    return count


def clean_non_incoming(db_path: str) -> int:
    """Delete classifications for self-transfers, ATM, broker."""
    conn = sqlite3.connect(db_path)
    
    patterns = [
        '%пётр николайович%',
        '%смирнов пётр%',
        '%г. пётр%',
        '%atm %',
        '%sberbank onl%vklad%',
        '%vklad-karta%',
        '%vklad_karta%',
        '%брокерск%',
        '%dbo transfer%',
    ]
    
    deleted = 0
    for p in patterns:
        cur = conn.execute("""
            DELETE FROM incoming_classifications
            WHERE income_tx_id IN (
                SELECT id FROM transactions 
                WHERE LOWER(description) LIKE ?
            )
        """, (p.lower(),))
        deleted += cur.rowcount
    
    conn.commit()
    conn.close()
    return deleted


def clean_known_sender_duplicates(db_path: str) -> int:
    """Remove duplicate patterns (T-Bank ****1688 vs Father_T-Bank)."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute("""
        DELETE FROM known_senders
        WHERE sender_pattern LIKE '%****%'
           OR sender_pattern LIKE 'Father_%'
           OR sender_pattern LIKE 'Mother_%'
    """)
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n
