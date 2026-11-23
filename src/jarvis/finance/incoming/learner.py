"""Learn from user confirmations."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime

from jarvis.finance.incoming.classifier import _extract_sender


def confirm_classification(
    db_path: str,
    income_tx_id: int,
    transfer_type: str,
    target_category: str | None = None,
    save_as_known: bool = True,
) -> None:
    """User confirmed classification. Save pattern for future auto-matching."""
    conn = sqlite3.connect(db_path)
    now = datetime.now(UTC).isoformat()

    conn.execute("""
        UPDATE incoming_classifications
        SET status = 'confirmed', confirmed_at = ?, transfer_type = ?, target_category = COALESCE(?, target_category)
        WHERE income_tx_id = ?
    """, (now, transfer_type, target_category, income_tx_id))

    row = conn.execute("""
        SELECT amount, description FROM transactions WHERE id = ?
    """, (income_tx_id,)).fetchone()
    if row:
        amount_val = float(row[0])
        desc = row[1] or ""
        desc_lower = desc.lower()
        sender = _extract_sender(desc)

        # Extract bank
        bank = None
        for b in ["t-bank", "tbank", "сбер", "sbol", "альфа", "alfabank", "vtb"]:
            if b in desc_lower:
                bank = b.upper() if b in ("sbol", "vtb") else "T-Bank" if "t" in b else b.capitalize()
                break

        # Extract account hint (e.g. ****1688 or **1933)
        acc_m = re.search(r"(\*{2,4}\d{4})", desc)
        acc_hint = acc_m.group(1) if acc_m else None

        if save_as_known and (bank or acc_hint or sender):
            pattern = sender or f"Learned_{bank or 'bank'}_{acc_hint or amount_val:.0f}"
            
            # Check existing pattern in known_senders
            existing = conn.execute("SELECT typical_amounts FROM known_senders WHERE sender_pattern = ?", (pattern,)).fetchone()
            amounts_list = [amount_val]
            if existing and existing[0]:
                try:
                    amounts_list = list(set(json.loads(existing[0]) + [amount_val]))
                except Exception:  # noqa: BLE001, S110
                    pass

            conn.execute("""
                INSERT INTO known_senders
                (sender_pattern, transfer_type, typical_category, bank_hint, typical_amounts, account_hint, note, confidence, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, 'learned', 0.9, ?, ?)
                ON CONFLICT(sender_pattern) DO UPDATE SET
                    transfer_type = excluded.transfer_type,
                    typical_category = COALESCE(excluded.typical_category, known_senders.typical_category),
                    bank_hint = COALESCE(excluded.bank_hint, known_senders.bank_hint),
                    typical_amounts = excluded.typical_amounts,
                    account_hint = COALESCE(excluded.account_hint, known_senders.account_hint),
                    last_seen_at = excluded.last_seen_at
            """, (pattern, transfer_type, target_category, bank, json.dumps(amounts_list), acc_hint, now, now))

        if sender:
            conn.execute("""
                INSERT INTO transfer_patterns
                (sender_pattern, keyword, transfer_type, typical_category,
                 times_confirmed, last_seen_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(sender_pattern, keyword, transfer_type)
                DO UPDATE SET
                    times_confirmed = times_confirmed + 1,
                    last_seen_at = excluded.last_seen_at
            """, (sender, None, transfer_type, target_category, now))

    conn.commit()
    conn.close()


def reject_classification(db_path: str, income_tx_id: int) -> None:
    """User rejected. Mark as unknown, do not learn."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        UPDATE incoming_classifications
        SET status = 'rejected', confirmed_at = ?
        WHERE income_tx_id = ?
    """, (datetime.now(UTC).isoformat(), income_tx_id))
    conn.commit()
    conn.close()
