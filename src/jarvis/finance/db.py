from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.domain import ActionRequest, ActionStatus, Transaction
from jarvis.finance.encrypted_sqlite import connect_sqlite
from jarvis.finance.ledger import (
    LEDGER_SCHEMA,
    fingerprint,
    record_source_event,
    transaction_payload,
)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_date TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'RUB',
    description TEXT NOT NULL DEFAULT '',
    merchant TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'uncategorized',
    account TEXT NOT NULL DEFAULT 'main',
    external_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(account, tx_date, amount, description, merchant, external_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    approval_required INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    cash_balance TEXT NOT NULL DEFAULT '0',
    reserve_balance TEXT NOT NULL DEFAULT '0',
    savings_balance TEXT NOT NULL DEFAULT '0',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_spending_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date TEXT NOT NULL,
    category TEXT NOT NULL,
    amount_spent TEXT NOT NULL,
    limit_amount TEXT NOT NULL DEFAULT '0',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS food_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'ration',
    unit TEXT NOT NULL DEFAULT 'kg',
    price_per_unit TEXT NOT NULL DEFAULT '0',
    daily_quantity TEXT NOT NULL DEFAULT '0',
    protein_g TEXT NOT NULL DEFAULT '0',
    calories TEXT NOT NULL DEFAULT '0',
    store TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS food_price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name TEXT NOT NULL,
    store TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL DEFAULT 'kg',
    price_per_unit TEXT NOT NULL DEFAULT '0',
    observed_date TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS savings_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    target_amount TEXT NOT NULL,
    current_amount TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL DEFAULT 3,
    deadline TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goal_contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    amount TEXT NOT NULL,
    contribution_date TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(goal_id) REFERENCES savings_goals(id)
);

CREATE TABLE IF NOT EXISTS budget_guardrails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guardrail_date TEXT NOT NULL,
    days_to_income INTEGER NOT NULL,
    free_cash TEXT NOT NULL,
    daily_safe_spend TEXT NOT NULL,
    recommended_freeze_categories TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS sber_import_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT 'sber',
    source_format TEXT NOT NULL DEFAULT '',
    parsed_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(file_hash, account)
);

CREATE TABLE IF NOT EXISTS ai_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_type TEXT NOT NULL,
    title TEXT NOT NULL,
    formula TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    confidence TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    task_type TEXT NOT NULL,
    interval_days INTEGER NOT NULL DEFAULT 1,
    next_run_date TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    run_date TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_food_price_observations_item ON food_price_observations(item_name);
CREATE INDEX IF NOT EXISTS idx_food_price_observations_date ON food_price_observations(observed_date);
CREATE INDEX IF NOT EXISTS idx_savings_goals_status_priority ON savings_goals(status, priority);
CREATE INDEX IF NOT EXISTS idx_goal_contributions_goal ON goal_contributions(goal_id);
CREATE INDEX IF NOT EXISTS idx_budget_guardrails_date ON budget_guardrails(guardrail_date);
CREATE INDEX IF NOT EXISTS idx_sber_import_files_hash ON sber_import_files(file_hash);
CREATE INDEX IF NOT EXISTS idx_ai_recommendations_type ON ai_recommendations(recommendation_type, status);
CREATE TABLE IF NOT EXISTS merchant_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    hits INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_merchant_rules_pattern ON merchant_rules(pattern);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_next_run ON scheduled_tasks(next_run_date);
CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_task ON scheduled_task_runs(task_id);

CREATE TABLE IF NOT EXISTS prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id INTEGER,
    merchant TEXT NOT NULL DEFAULT '',
    predicted_category TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    used_qwen INTEGER NOT NULL DEFAULT 0,
    user_corrected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prediction_log_created ON prediction_log(created_at);
CREATE INDEX IF NOT EXISTS idx_prediction_log_tx_id ON prediction_log(tx_id);

CREATE TABLE IF NOT EXISTS known_senders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_pattern TEXT NOT NULL UNIQUE,
    transfer_type TEXT NOT NULL,
    typical_category TEXT,
    bank_hint TEXT,
    typical_amounts TEXT,
    account_hint TEXT,
    note TEXT,
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incoming_classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    income_tx_id INTEGER NOT NULL UNIQUE,
    transfer_type TEXT NOT NULL,
    matched_expense_tx_id INTEGER,
    target_category TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'pending',
    reasoning TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    FOREIGN KEY (income_tx_id) REFERENCES transactions(id),
    FOREIGN KEY (matched_expense_tx_id) REFERENCES transactions(id)
);

CREATE TABLE IF NOT EXISTS transfer_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_pattern TEXT NOT NULL,
    keyword TEXT,
    transfer_type TEXT NOT NULL,
    typical_category TEXT,
    times_confirmed INTEGER DEFAULT 1,
    last_seen_at TEXT NOT NULL,
    UNIQUE(sender_pattern, keyword, transfer_type)
);

CREATE TABLE IF NOT EXISTS question_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    amount REAL,
    description TEXT,
    question_text TEXT NOT NULL,
    suggested_answers TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 5,
    created_at TEXT NOT NULL,
    answered_at TEXT,
    user_answer TEXT
);

CREATE TABLE IF NOT EXISTS outgoing_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_pattern TEXT,
    amount_min REAL,
    amount_max REAL,
    typical_category TEXT NOT NULL,
    user_label TEXT,
    times_confirmed INTEGER DEFAULT 1,
    auto_classify_after INTEGER DEFAULT 5,
    negative_signals INTEGER DEFAULT 0,
    created_at TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS tx_embeddings (
    tx_id INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    amount REAL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS classification_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL,
    wrong_category TEXT NOT NULL,
    correct_category TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_incoming_status ON incoming_classifications(status);
CREATE INDEX IF NOT EXISTS idx_incoming_type ON incoming_classifications(transfer_type);
CREATE INDEX IF NOT EXISTS idx_question_queue_status ON question_queue(status, priority);

CREATE TABLE IF NOT EXISTS sms_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    regex_pattern TEXT NOT NULL,
    example_text TEXT,
    parsed_fields TEXT,
    match_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sms_patterns_count 
    ON sms_patterns(match_count DESC);
"""


KNOWN_SENDERS_SEED = [
    ("Николай Смирнов", "parental_support", "housing_utilities", None, None, None, "отец"),
    ("Смирнов Николай", "parental_support", "housing_utilities", None, None, None, "отец"),
    ("Ольга Смирнова", "parental_support", "food_basic", None, None, None, "мать"),
    ("Смирнова Ольга", "parental_support", "food_basic", None, None, None, "мать"),
    ("Father_T-Bank", "parental_support", "housing_utilities", "T-Bank", json.dumps([12800.0, 13000.0, 26000.0]), None, "отец (T-Bank)"),
    ("Mother_SBOL", "parental_support", "food_basic", "SBOL", json.dumps([5000.0, 10000.0]), None, "мать (SBOL)"),
]


FAMILY_SEED = [
    ("николай николаевич", "parental_support", "отец", "housing_utilities"),
    ("ольга николаевна", "parental_support", "мать", "food_basic"),
    ("дамира сагитовна", "gift", "бабушка Дамира", None),
    ("равиля сергейхановна", "gift", "бабушка Равиля", None),
    ("надир сайярович", "compensation", "брат Надир", None),
    ("александр александрович", "compensation", "друг Александр", None),
    ("дмитрий сергеевич", "compensation", "друг Дмитрий", None),
    ("ярослав вячеславович", "compensation", "друг Ярослав", None),
    ("максим александрович", "compensation", "друг Максим", None),
]


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, ddl: str) -> None:
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


class FinanceDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.executescript(LEDGER_SCHEMA)
            for col, ddl in [
                ("bank_hint", "bank_hint TEXT"),
                ("typical_amounts", "typical_amounts TEXT"),
                ("account_hint", "account_hint TEXT"),
                ("sender_label", "sender_label TEXT"),
                ("name_pattern", "name_pattern TEXT"),
                ("amount_min", "amount_min REAL"),
                ("amount_max", "amount_max REAL"),
                ("day_of_month", "day_of_month INTEGER"),
                ("context_description", "context_description TEXT"),
                ("times_confirmed", "times_confirmed INTEGER DEFAULT 1"),
                ("auto_classify_after", "auto_classify_after INTEGER DEFAULT 5"),
                ("direction", "direction TEXT DEFAULT 'incoming'"),
                ("negative_signals", "negative_signals INTEGER DEFAULT 0"),
            ]:
                _add_column_if_missing(conn, "known_senders", col, ddl)

            now = utc_now()
            for item in KNOWN_SENDERS_SEED:
                if len(item) == 7:
                    pattern, ttype, cat, bank_hint, typ_amounts, acc_hint, note = item
                else:
                    pattern, ttype, cat, note = item
                    bank_hint, typ_amounts, acc_hint = None, None, None
                conn.execute("""
                    INSERT INTO known_senders
                    (sender_pattern, transfer_type, typical_category, bank_hint, typical_amounts, account_hint, note, confidence, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?)
                    ON CONFLICT(sender_pattern) DO NOTHING
                """, (pattern, ttype, cat, bank_hint, typ_amounts, acc_hint, note, now, now))
            self.seed_family_patterns(conn)
            self.audit(conn, "db_initialized", {"db": str(self.path)})

    def seed_family_patterns(self, conn: sqlite3.Connection | None = None) -> int:
        if conn is not None:
            return self._seed_family_patterns_conn(conn)
        with self.connect() as c:
            return self._seed_family_patterns_conn(c)

    def _seed_family_patterns_conn(self, conn: sqlite3.Connection) -> int:
        now = utc_now()
        n = 0
        for name_pattern, ttype, label, cat in FAMILY_SEED:
            try:
                conn.execute("""
                    INSERT INTO known_senders
                    (sender_pattern, name_pattern, transfer_type, typical_category,
                     sender_label, note, confidence, created_at, last_seen_at,
                     times_confirmed, auto_classify_after, direction)
                    VALUES (?, ?, ?, ?, ?, 'family_seed', 0.95, ?, ?, 1, 5, 'incoming')
                    ON CONFLICT(sender_pattern) DO NOTHING
                """, (
                    f"name:{name_pattern}", name_pattern, ttype, cat,
                    label, now, now,
                ))
                n += 1
            except sqlite3.IntegrityError:
                pass
        return n

    def audit(self, conn: sqlite3.Connection, event_type: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(event_type, payload, created_at) VALUES (?, ?, ?)",
            (event_type, json.dumps(payload, ensure_ascii=False), utc_now()),
        )

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self.audit(conn, "setting_saved", {"key": key})

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if row is None:
                return default
            return json.loads(row["value"])

    def add_transactions(self, transactions: Iterable[Transaction], source: str = "import", parser_version: str = "1") -> int:
        inserted = 0
        with self.connect() as conn:
            for tx in transactions:
                if self._insert_transaction(conn, tx, source, parser_version):
                    inserted += 1
            self.audit(conn, "transactions_imported", {"inserted": inserted})
        return inserted

    def add_transaction(self, transaction: Transaction, source: str = "manual", parser_version: str = "1") -> int:
        with self.connect() as conn:
            inserted = self._insert_transaction(conn, transaction, source, parser_version)
            if inserted:
                self.audit(conn, "transaction_added", {"source": source, "amount": str(transaction.amount)})
                row = conn.execute("SELECT id FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
                return int(row["id"])
            self.audit(conn, "transaction_duplicate_skipped", {"source": source})
            return 0

    def _insert_transaction(self, conn: sqlite3.Connection, tx: Transaction, source: str = "manual", parser_version: str = "1") -> bool:
        if not tx.amount.is_finite() or not tx.currency or not source or not parser_version:
            raise ValueError("Transaction amount, currency and provenance must be valid")
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        payload = transaction_payload(tx)
        existing: list[sqlite3.Row] = []
        if tx.external_id:
            existing = conn.execute(
                "SELECT * FROM transactions WHERE account = ? AND external_id = ?",
                (tx.account, tx.external_id),
            ).fetchall()
        elif source != "manual":
            candidates = conn.execute(
                "SELECT * FROM transactions WHERE account = ? AND tx_date = ?",
                (tx.account, tx.tx_date.isoformat()),
            ).fetchall()
            existing = [row for row in candidates if fingerprint(dict(row)) == fingerprint(payload)]
        repeated_row = bool(tx.external_id and re.fullmatch(
            r"[a-f0-9]{24}-[1-9][0-9]*", tx.external_id))
        if not existing and tx.external_id and source != "manual" and not repeated_row:
            candidates = conn.execute(
                "SELECT * FROM transactions WHERE account = ? AND tx_date = ? AND currency = ?",
                (tx.account, tx.tx_date.isoformat(), tx.currency),
            ).fetchall()
            ambiguous = [row for row in candidates if Decimal(row["amount"]) == tx.amount
                         and ((tx.merchant and row["merchant"] == tx.merchant)
                              or (tx.description and row["description"] == tx.description))]
            ambiguous = [row for row in ambiguous if not conn.execute(
                "SELECT 1 FROM source_events WHERE transaction_id = ? AND source = ? AND status = 'accepted'",
                (row["id"], source),
            ).fetchone()]
            if ambiguous:
                record_source_event(conn, tx, int(ambiguous[0]["id"]), source, parser_version, "pending_review")
                return False
        if existing:
            status = "accepted" if len(existing) == 1 and fingerprint(dict(existing[0])) == fingerprint(payload) else "pending_review"
            record_source_event(conn, tx, int(existing[0]["id"]), source, parser_version, status)
            return False
        try:
            conn.execute(
                """
                INSERT INTO transactions(
                    tx_date, amount, currency, description, merchant,
                    category, account, external_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx.tx_date.isoformat(),
                    str(tx.amount),
                    tx.currency,
                    tx.description,
                    tx.merchant,
                    tx.category,
                    tx.account,
                    tx.external_id,
                    utc_now(),
                ),
            )
            tx_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            record_source_event(conn, tx, tx_id, source, parser_version, "accepted")
            return True
        except sqlite3.IntegrityError:
            return False

    def list_transactions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions ORDER BY tx_date DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def transactions_between(self, start: date, end: date) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM transactions
                WHERE tx_date >= ? AND tx_date <= ?
                ORDER BY tx_date ASC, id ASC
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            return self._attach_allocations(conn, rows)

    def all_transactions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM transactions ORDER BY tx_date ASC, id ASC").fetchall()
            return self._attach_allocations(conn, rows)

    @staticmethod
    def _attach_allocations(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        allocations = conn.execute("SELECT * FROM transaction_allocations ORDER BY position").fetchall()
        result = [dict(row) for row in rows]
        by_id: dict[int, list[dict[str, Any]]] = {}
        for allocation in allocations:
            by_id.setdefault(int(allocation["transaction_id"]), []).append(dict(allocation))
        for row in result:
            if row["id"] in by_id:
                row["allocations"] = by_id[row["id"]]
        return result

    def set_transaction_allocations(self, tx_id: int, parts: list[tuple[str, Decimal]]) -> None:
        """Split economic categories without duplicating the account event."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT amount FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if row is None:
                raise ValueError("Transaction not found")
            amount = Decimal(row["amount"])
            if not parts or any(not value.is_finite() or not category for category, value in parts):
                raise ValueError("Allocations must have valid amounts and categories")
            if sum((value for _, value in parts), Decimal(0)) != amount:
                raise ValueError("Allocation sum differs from transaction amount")
            conn.execute("DELETE FROM transaction_allocations WHERE transaction_id = ?", (tx_id,))
            conn.executemany("INSERT INTO transaction_allocations VALUES (?, ?, ?, ?)",
                             [(tx_id, pos, category, str(value)) for pos, (category, value) in enumerate(parts)])
            self.audit(conn, "transaction_split", {"tx_id": tx_id, "parts": len(parts)})

    def record_account_balance(self, account: str, currency: str, asof_date: date, amount: Decimal, source: str = "manual") -> None:
        """Record an explicit end-of-day balance without guessing an opening."""
        if not amount.is_finite() or not account or not currency or not source:
            raise ValueError("Valid balance and provenance required")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT amount FROM account_balance_observations WHERE account=? AND currency=? AND asof_date=? AND source=?", (account, currency, asof_date.isoformat(), source)).fetchone()
            if existing:
                if Decimal(existing["amount"]) != amount:
                    raise ValueError("Conflicting balance observation; preserve original and review")
                return
            conn.execute("INSERT INTO account_balance_observations(account,currency,asof_date,amount,source,created_at) VALUES (?,?,?,?,?,?)", (account, currency, asof_date.isoformat(), str(amount), source, utc_now()))

    def balance(self) -> Decimal:
        with self.connect() as conn:
            rows = conn.execute("SELECT amount, currency FROM transactions").fetchall()
        if len({row["currency"] for row in rows}) > 1:
            raise ValueError("Cannot sum balances across currencies without exchange rates")
        return sum((Decimal(row["amount"]) for row in rows), Decimal(0)).quantize(Decimal("0.01"))

    def create_action_request(
        self,
        action_type: str,
        payload: dict[str, Any],
        reason: str = "",
        approval_required: bool = True,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO action_requests(action_type, payload, status, reason, approval_required, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_type,
                    json.dumps(payload, ensure_ascii=False),
                    ActionStatus.PENDING.value,
                    reason,
                    1 if approval_required else 0,
                    now,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            action_id = int(cur.lastrowid)
            self.audit(conn, "action_request_created", {"id": action_id, "action_type": action_type})
            return action_id

    def list_action_requests(self, status: str | None = None, limit: int = 50) -> list[ActionRequest]:
        query = "SELECT * FROM action_requests"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row_to_action_request(row) for row in rows]

    def get_action_request(self, action_id: int) -> ActionRequest | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM action_requests WHERE id = ?", (action_id,)).fetchone()
        return None if row is None else row_to_action_request(row)

    def update_action_status(self, action_id: int, status: ActionStatus | str) -> bool:
        status_value = status.value if isinstance(status, ActionStatus) else str(status)
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE action_requests SET status = ?, updated_at = ? WHERE id = ?",
                (status_value, utc_now(), action_id),
            )
            updated = cur.rowcount > 0
            if updated:
                self.audit(conn, "action_request_status_changed", {"id": action_id, "status": status_value})
            return updated

    def add_budget_snapshot(
        self,
        snapshot_date: date,
        cash_balance: Decimal,
        reserve_balance: Decimal,
        savings_balance: Decimal,
        notes: str = "",
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO budget_snapshots(snapshot_date, cash_balance, reserve_balance, savings_balance, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_date.isoformat(),
                    str(cash_balance),
                    str(reserve_balance),
                    str(savings_balance),
                    notes,
                    utc_now(),
                ),
            )
            assert cur.lastrowid is not None
            snapshot_id = int(cur.lastrowid)
            self.audit(conn, "budget_snapshot_added", {"id": snapshot_id, "date": snapshot_date.isoformat()})
            return snapshot_id

    def latest_budget_snapshot(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM budget_snapshots ORDER BY snapshot_date DESC, id DESC LIMIT 1").fetchone()
        return None if row is None else dict(row)

    def list_budget_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM budget_snapshots ORDER BY snapshot_date DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def add_category_spending_entry(
        self,
        entry_date: date,
        category: str,
        amount_spent: Decimal,
        limit_amount: Decimal,
        notes: str = "",
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO category_spending_entries(entry_date, category, amount_spent, limit_amount, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (entry_date.isoformat(), category, str(amount_spent), str(limit_amount), notes, utc_now()),
            )
            assert cur.lastrowid is not None
            entry_id = int(cur.lastrowid)
            self.audit(conn, "category_spending_entry_added", {"id": entry_id, "category": category})
            return entry_id

    def list_category_spending_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM category_spending_entries ORDER BY entry_date DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_or_update_food_item(
        self,
        name: str,
        unit: str,
        price_per_unit: Decimal,
        daily_quantity: Decimal = Decimal(0),
        protein_g: Decimal = Decimal(0),
        calories: Decimal = Decimal(0),
        store: str = "",
        category: str = "ration",
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO food_items(name, category, unit, price_per_unit, daily_quantity, protein_g, calories, store, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    category=excluded.category,
                    unit=excluded.unit,
                    price_per_unit=excluded.price_per_unit,
                    daily_quantity=excluded.daily_quantity,
                    protein_g=excluded.protein_g,
                    calories=excluded.calories,
                    store=excluded.store,
                    updated_at=excluded.updated_at
                """,
                (
                    name,
                    category,
                    unit,
                    str(price_per_unit),
                    str(daily_quantity),
                    str(protein_g),
                    str(calories),
                    store,
                    now,
                ),
            )
            row = conn.execute("SELECT id FROM food_items WHERE name = ?", (name,)).fetchone()
            item_id = int(row["id"])
            self.audit(conn, "food_item_saved", {"id": item_id, "name": name})
            return item_id

    def list_food_items(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM food_items ORDER BY name ASC").fetchall()
        return [dict(row) for row in rows]

    def ration_daily_cost(self) -> Decimal:
        total = Decimal("0.00")
        for item in self.list_food_items():
            total += Decimal(str(item["price_per_unit"])) * Decimal(str(item["daily_quantity"]))
        return total.quantize(Decimal("0.01"))

    def add_food_price_observation(
        self,
        item_name: str,
        store: str,
        unit: str,
        price_per_unit: Decimal,
        observed_date: date,
        notes: str = "",
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO food_price_observations(item_name, store, unit, price_per_unit, observed_date, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (item_name, store, unit, str(price_per_unit), observed_date.isoformat(), notes, utc_now()),
            )
            assert cur.lastrowid is not None
            price_id = int(cur.lastrowid)
            self.audit(conn, "food_price_observation_added", {"id": price_id, "item_name": item_name, "store": store})
            return price_id

    def latest_food_price(self, item_name: str, store: str | None = None) -> float:
        with self.connect() as conn:
            if store:
                row = conn.execute(
                    "SELECT price_per_unit FROM food_price_observations "
                    "WHERE item_name = ? AND store = ? "
                    "ORDER BY observed_at DESC LIMIT 1",
                    (item_name, store)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT price_per_unit, store FROM food_price_observations "
                    "WHERE item_name = ? "
                    "ORDER BY price_per_unit ASC LIMIT 1",
                    (item_name,)
                ).fetchone()
        return float(row['price_per_unit']) if row else 0.0

    def list_food_price_observations(self, item_name: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: list[Any] = []
        query = "SELECT * FROM food_price_observations"
        if item_name:
            query += " WHERE item_name = ?"
            params.append(item_name)
        query += " ORDER BY observed_date DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def add_or_update_savings_goal(
        self,
        name: str,
        target_amount: Decimal,
        current_amount: Decimal = Decimal(0),
        priority: int = 3,
        deadline: date | None = None,
        notes: str = "",
        status: str = "active",
        *, metadata: dict[str, Any] | None = None,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO savings_goals(name, target_amount, current_amount, priority, deadline, status, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    target_amount=excluded.target_amount,
                    current_amount=excluded.current_amount,
                    priority=excluded.priority,
                    deadline=excluded.deadline,
                    status=excluded.status,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (
                    name,
                    str(target_amount),
                    str(current_amount),
                    int(priority),
                    deadline.isoformat() if deadline else None,
                    status,
                    notes,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT id FROM savings_goals WHERE name = ?", (name,)).fetchone()
            goal_id = int(row["id"])
            if metadata is not None:
                conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("cfo.goal_plan:" + str(goal_id), json.dumps(metadata, ensure_ascii=False, allow_nan=False)))
            self.audit(conn, "savings_goal_saved", {"id": goal_id, "name": name})
            return goal_id

    def list_savings_goals(self, status: str | None = "active") -> list[dict[str, Any]]:
        query = "SELECT * FROM savings_goals"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY status ASC, priority ASC, COALESCE(deadline, '9999-12-31') ASC, id ASC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_savings_goal(self, goal_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM savings_goals WHERE id = ?", (goal_id,)).fetchone()
        return None if row is None else dict(row)

    def add_goal_contribution(
        self,
        goal_id: int,
        amount: Decimal,
        contribution_date: date,
        source: str = "manual",
        notes: str = "",
    ) -> int:
        with self.connect() as conn:
            goal = conn.execute("SELECT * FROM savings_goals WHERE id = ?", (goal_id,)).fetchone()
            if goal is None:
                raise ValueError(f"Savings goal #{goal_id} does not exist")
            cur = conn.execute(
                """
                INSERT INTO goal_contributions(goal_id, amount, contribution_date, source, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (goal_id, str(amount), contribution_date.isoformat(), source, notes, utc_now()),
            )
            new_current = Decimal(str(goal["current_amount"])) + amount
            target = Decimal(str(goal["target_amount"]))
            status = "completed" if new_current >= target else str(goal["status"])
            conn.execute(
                "UPDATE savings_goals SET current_amount = ?, status = ?, updated_at = ? WHERE id = ?",
                (str(new_current.quantize(Decimal("0.01"))), status, utc_now(), goal_id),
            )
            assert cur.lastrowid is not None
            contribution_id = int(cur.lastrowid)
            self.audit(conn, "goal_contribution_added", {"id": contribution_id, "goal_id": goal_id, "amount": str(amount)})
            return contribution_id

    def list_goal_contributions(self, goal_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM goal_contributions"
        params: list[Any] = []
        if goal_id is not None:
            query += " WHERE goal_id = ?"
            params.append(goal_id)
        query += " ORDER BY contribution_date DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def add_budget_guardrail(
        self,
        guardrail_date: date,
        days_to_income: int,
        free_cash: Decimal,
        daily_safe_spend: Decimal,
        recommended_freeze_categories: list[str],
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO budget_guardrails(guardrail_date, days_to_income, free_cash, daily_safe_spend, recommended_freeze_categories, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    guardrail_date.isoformat(),
                    int(days_to_income),
                    str(free_cash),
                    str(daily_safe_spend),
                    json.dumps(recommended_freeze_categories, ensure_ascii=False),
                    utc_now(),
                ),
            )
            assert cur.lastrowid is not None
            guardrail_id = int(cur.lastrowid)
            self.audit(conn, "budget_guardrail_added", {"id": guardrail_id, "daily_safe_spend": str(daily_safe_spend)})
            return guardrail_id

    def list_budget_guardrails(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM budget_guardrails ORDER BY guardrail_date DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def create_scheduled_task(
        self,
        name: str,
        task_type: str,
        interval_days: int,
        next_run_date: date,
        params: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO scheduled_tasks(name, task_type, interval_days, next_run_date, params, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    task_type,
                    int(interval_days),
                    next_run_date.isoformat(),
                    json.dumps(params or {}, ensure_ascii=False),
                    1 if enabled else 0,
                    now,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            task_id = int(cur.lastrowid)
            self.audit(conn, "scheduled_task_created", {"id": task_id, "task_type": task_type})
            return task_id

    def list_scheduled_tasks(self, enabled_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM scheduled_tasks"
        params: list[Any] = []
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY next_run_date ASC, id ASC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_scheduled_task_next_run(self, task_id: int, next_run_date: date) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE scheduled_tasks SET next_run_date = ?, updated_at = ? WHERE id = ?",
                (next_run_date.isoformat(), utc_now(), task_id),
            )
            updated = cur.rowcount > 0
            if updated:
                self.audit(conn, "scheduled_task_rescheduled", {"id": task_id, "next_run_date": next_run_date.isoformat()})
            return updated

    def record_scheduled_task_run(self, task_id: int, run_date: date, status: str, message: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO scheduled_task_runs(task_id, run_date, status, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, run_date.isoformat(), status, message, utc_now()),
            )
            assert cur.lastrowid is not None
            run_id = int(cur.lastrowid)
            self.audit(conn, "scheduled_task_run_recorded", {"id": run_id, "task_id": task_id, "status": status})
            return run_id

    def list_scheduled_task_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_task_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_uncategorized_transactions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM transactions
                WHERE category IS NULL OR category = '' OR category = 'uncategorized'
                ORDER BY tx_date DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_ai_recommendation(
        self,
        recommendation_type: str,
        title: str,
        formula: str,
        payload: dict[str, Any],
        confidence: Decimal,
        status: str = "open",
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ai_recommendations(recommendation_type, title, formula, payload, confidence, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recommendation_type,
                    title,
                    formula,
                    json.dumps(payload, ensure_ascii=False),
                    str(confidence),
                    status,
                    utc_now(),
                ),
            )
            assert cur.lastrowid is not None
            rec_id = int(cur.lastrowid)
            self.audit(conn, "ai_recommendation_added", {"id": rec_id, "type": recommendation_type})
            return rec_id

    def list_ai_recommendations(self, status: str | None = "open", limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM ai_recommendations"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.get("payload") or "{}")
            result.append(item)
        return result

    def list_audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def add_merchant_rule(self, pattern: str, category: str, source: str = "manual") -> int:
        now = utc_now()
        p = pattern.lower().strip()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM merchant_rules WHERE pattern = ?", (p,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE merchant_rules SET category = ?, source = ?, updated_at = ? WHERE id = ?",
                    (category, source, now, row["id"])
                )
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO merchant_rules (pattern, category, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (p, category, source, now, now)
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def find_merchant_rule(self, description: str) -> str | None:
        if not description:
            return None
        d = description.lower()
        with self.connect() as conn:
            rules = conn.execute(
                "SELECT pattern, category FROM merchant_rules ORDER BY length(pattern) DESC"
            ).fetchall()
        for r in rules:
            if r["pattern"] in d:
                return str(r["category"])
        return None

    def increment_rule_hits(self, pattern: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE merchant_rules SET hits = hits + 1, updated_at = ? WHERE pattern = ?",
                (utc_now(), pattern.lower())
            )

    def list_merchant_rules(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, pattern, category, source, hits, created_at "
                "FROM merchant_rules ORDER BY hits DESC, updated_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_merchant_rule(self, rule_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM merchant_rules WHERE id = ?", (rule_id,))
            return cur.rowcount > 0

    def confirm_transaction_category(self, tx_id: int, category: str) -> bool:
        """Record explicit human feedback even when the suggested label was correct."""
        if not category.strip() or category in {"unknown", "uncategorized"}:
            raise ValueError("A confirmed category is required")
        return self.update_transaction_category(tx_id, category, source="manual", confirm_unchanged=True)

    def update_transaction_category(
        self, tx_id: int, category: str, source: str = "manual", *, confirm_unchanged: bool = False,
    ) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT category FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if previous is None:
                return False
            if previous["category"] == category and not confirm_unchanged:
                return True
            version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM category_corrections WHERE transaction_id = ?", (tx_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO category_corrections(transaction_id, version, old_category, new_category, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (tx_id, version, previous["category"], category, source, utc_now()),
            )
            cur = conn.execute(
                "UPDATE transactions SET category = ? WHERE id = ?",
                (category, tx_id)
            )
            if cur.rowcount > 0:
                self.audit(conn, "category_updated", {
                    "tx_id": tx_id, "new_category": category, "source": source
                })
                return True
        return False

    def log_prediction(
        self,
        tx_id: int | str | None,
        merchant: str,
        predicted_category: str,
        confidence: float,
        used_qwen: bool = False,
        user_corrected: bool = False,
    ) -> int:
        tx_id_val = int(tx_id) if tx_id is not None else None
        now = utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO prediction_log(
                    tx_id, merchant, predicted_category, confidence,
                    used_qwen, user_corrected, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx_id_val,
                    merchant or "",
                    predicted_category,
                    float(confidence),
                    1 if used_qwen else 0,
                    1 if user_corrected else 0,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def mark_correction(self, tx_id: int | str, corrected_category: str | None = None) -> bool:
        tx_id_val = int(tx_id)
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE prediction_log SET user_corrected = 1 WHERE tx_id = ?",
                (tx_id_val,),
            )
            return cur.rowcount > 0


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def row_to_action_request(row: sqlite3.Row) -> ActionRequest:
    return ActionRequest(
        id=int(row["id"]),
        action_type=str(row["action_type"]),
        payload=json.loads(row["payload"]),
        status=ActionStatus(str(row["status"])),
        reason=str(row["reason"]),
        approval_required=bool(row["approval_required"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
