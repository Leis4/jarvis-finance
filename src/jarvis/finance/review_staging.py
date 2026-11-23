"""Guarded rehearsal on disposable copies; no production destination API."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.domain import Transaction
from jarvis.finance.ledger import LEDGER_SCHEMA, record_source_event
from jarvis.finance.review_store import ReviewStore, encoded

JOURNAL = """
CREATE TABLE IF NOT EXISTS rehearsal_events (
    event_key TEXT PRIMARY KEY, batch_id TEXT NOT NULL, transaction_id INTEGER,
    source_event_id INTEGER, source_sha256 TEXT NOT NULL, parser_version TEXT NOT NULL,
    original_record TEXT NOT NULL, inserted_record TEXT NOT NULL, decision TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('inserted','rolled_back'))
);
"""


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_staging_plan(store: ReviewStore) -> dict[str, Any]:
    """Export explicit decisions and provenance without accepting production writes."""
    meta = store.metadata()
    if json.loads(meta["packet"]).get("source_kind") in {"sber_mcp", "generated_pdf"}:
        generated = json.loads(meta["packet"]).get("source_kind") == "generated_pdf"
        return {"status": "blocked", "reason": "Generated document financial source unapproved" if generated else "Native source financial semantics/apply not accepted",
                "packet_hash": meta["packet_hash"], "proposals": [], "apply_allowed": False,
                "production_database_modified": False}
    approved = [i for i in store.items() if i["status"] == "approve_new"]
    return {"packet_hash": meta["packet_hash"], "parser_version": meta["parser_version"],
            "production_database_modified": False, "apply_allowed": False,
            "proposals": [{"item_id": i["id"], "revision": i["revision"], "decision": i["decision"],
                           "source_sha256": i["source_sha256"], "source_ordinal": i["pdf_index"] + 1,
                           "bank_account": i["bank_account"], "local_account": i["local_account"],
                           "original_record": i["operation"], "apply_allowed": False,
                           "idempotency_key": hashlib.sha256(f"{i['source_sha256']}:{meta['parser_version']}:{i['pdf_index']}".encode()).hexdigest()}
                          for i in approved]}


def _guard(store: ReviewStore, batch_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
        raise ValueError("Invalid rehearsal batch")
    root = (store.workspace / "rehearsals").resolve()
    path = root / batch_id / "candidate.sqlite"
    if not path.is_file() or not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError("Outside managed rehearsal copy")
    marker = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
    if marker["batch_id"] != batch_id or marker["packet_hash"] != store.metadata()["packet_hash"]:
        raise ValueError("Rehearsal identity mismatch")
    return path


def _apply(store: ReviewStore, path: Path, batch_id: str,
           reviewed_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Apply only explicit new-event decisions to an already guarded copy."""
    items = [i for i in (store.items() if reviewed_items is None else reviewed_items) if i["status"] == "approve_new"]
    version = store.metadata()["parser_version"]
    inserted = 0
    skipped = 0
    held: list[dict[str, str]] = []
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for item in items:
                raw = item["operation"]
                key = hashlib.sha256(f"{item['source_sha256']}:{version}:{item['pdf_index']}".encode()).hexdigest()
                old = conn.execute("SELECT * FROM rehearsal_events WHERE event_key=?", (key,)).fetchone()
                if old:
                    if old["decision"] != encoded(item["decision"]) or old["parser_version"] != version:
                        raise ValueError("Rehearsal decision/parser changed; replay requires a new batch")
                    if old["status"] != "inserted" or old["original_record"] != encoded(raw):
                        raise ValueError("Rolled-back or changed event cannot be replayed silently")
                    current = conn.execute("SELECT * FROM transactions WHERE id=?", (old["transaction_id"],)).fetchone()
                    if current is None or encoded(dict(current)) != old["inserted_record"]:
                        raise ValueError("Rehearsal event changed; manual review required")
                    skipped += 1
                    continue
                amount = Decimal(raw["amount"])
                if not amount.is_finite() or raw["account"] != item["bank_account"]:
                    raise ValueError("Invalid financial source identity")
                peers = conn.execute("SELECT * FROM transactions WHERE tx_date=? AND currency=?",
                                     (raw["tx_date"], raw["currency"])).fetchall()
                # Shared financial evidence across accounts/statement versions blocks insertion.
                if any(Decimal(peer["amount"]) == amount for peer in peers):
                    held.append({"item_id": item["id"], "reason": "overlapping financial evidence; keep pending_review"})
                    continue
                external = f"pdf:{item['source_sha256']}:{item['pdf_index']}"
                tx = Transaction(date.fromisoformat(raw["tx_date"]), amount, raw["currency"], raw["description"],
                                 raw["merchant"], raw["category"], item["local_account"], external)
                cursor = conn.execute("""INSERT INTO transactions
                    (tx_date,amount,currency,description,merchant,category,account,external_id,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (tx.tx_date.isoformat(), str(tx.amount), tx.currency,
                    tx.description, tx.merchant, tx.category, tx.account, external, datetime.now(UTC).isoformat()))
                ident = cursor.lastrowid
                record_source_event(conn, tx, ident, "pdf_manual_review", version, "accepted")
                source_id = conn.execute("SELECT id FROM source_events WHERE transaction_id=? AND event_key=?",
                                         (ident, external)).fetchone()[0]
                inserted_row = dict(conn.execute("SELECT * FROM transactions WHERE id=?", (ident,)).fetchone())
                conn.execute("INSERT INTO rehearsal_events VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    key, batch_id, ident, source_id, item["source_sha256"], version, encoded(raw),
                    encoded(inserted_row), encoded(item["decision"]), "inserted"))
                inserted += 1
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Rehearsal referential integrity failure")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"inserted": inserted, "idempotent_skips": skipped, "held": held,
            "approved_new": len(items), "apply_allowed": False, "production_database_modified": False}


def rehearse(store: ReviewStore, snapshot: Path, statements: Path) -> dict[str, Any]:
    """Create a copy, exercise replay, and verify source/backup identities."""
    meta = store.metadata()
    if json.loads(meta["packet"]).get("source_kind") in {"sber_mcp", "generated_pdf"}:
        generated = json.loads(meta["packet"]).get("source_kind") == "generated_pdf"
        raise ValueError("Generated document financial apply not accepted; review only" if generated else "Native financial apply not accepted; review only")
    from jarvis.finance.review_backup import review_snapshot
    from jarvis.finance.review_provenance import decision_binding
    with review_snapshot(store) as frozen:
        binding = decision_binding(frozen)
        reviewed_items = frozen.items()
    packet = json.loads(meta["packet"])
    source_hash = _hash(snapshot)
    if source_hash != packet["snapshot_sha256"]:
        raise ValueError("Snapshot hash changed; regenerate evidence before rehearsal")
    for source in packet["pdf_sources"]:
        name = source["file"]
        if Path(name).name != name or _hash(statements / name) != source["sha256"]:
            raise ValueError("Original statement hash/name mismatch")
    from jarvis.finance.bank import sber_statement
    if meta["parser_version"] != "sber-pdf:" + _hash(Path(sber_statement.__file__)):
        raise ValueError("Parser changed since review; use a new packet/workspace")
    batch = uuid.uuid4().hex
    root = store.workspace / "rehearsals" / batch
    root.mkdir(parents=True, exist_ok=False)
    backup = root / "before.sqlite"
    with (closing(sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)) as source_conn,
          closing(sqlite3.connect(backup)) as copy):
        source_conn.backup(copy)
    path = root / "candidate.sqlite"
    shutil.copyfile(backup, path)
    manifest = {"batch_id": batch, "packet_hash": meta["packet_hash"], "snapshot_sha256": source_hash,
                "backup_sha256": _hash(backup), "production_database_modified": False,
                "source_snapshot_path": str(snapshot.resolve()), "source_directory": str(statements.resolve()),
                "provenance": binding}
    (root / "manifest.json").write_text(encoded(manifest), encoding="utf-8")
    path = _guard(store, batch)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(LEDGER_SCHEMA + JOURNAL)
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Copy integrity check failed")
    first = _apply(store, path, batch, reviewed_items)
    replay = _apply(store, path, batch, reviewed_items)
    manifest["candidate_sha256"] = _hash(path)
    (root / "manifest.json").write_text(encoded(manifest), encoding="utf-8")
    if replay["inserted"]:
        raise ValueError("Rehearsal replay added duplicates")
    restored = root / "restore-verification.sqlite"
    shutil.copyfile(backup, restored)
    if _hash(restored) != manifest["backup_sha256"] or _hash(snapshot) != source_hash:
        raise ValueError("Source or backup changed during rehearsal")
    report = {**manifest, "first": first, "replay": replay, "restore_verified": True,
              "status": "held_for_review" if first["held"] else "copy_only_rehearsed" if first["approved_new"] else "no_approved_events",
              "coverage_accepted": False, "financial_balance_accepted": False, "apply_allowed": False}
    (root / "report.json").write_text(encoded(report), encoding="utf-8")
    return report


def rollback_rehearsal(store: ReviewStore, batch_id: str) -> dict[str, Any]:
    """Undo only unchanged batch inserts on the managed disposable copy."""
    path = _guard(store, batch_id)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute("SELECT * FROM rehearsal_events WHERE batch_id=? AND status='inserted'", (batch_id,)).fetchall()
            for entry in rows:
                ident = entry["transaction_id"]
                current = conn.execute("SELECT * FROM transactions WHERE id=?", (ident,)).fetchone()
                if current is None or encoded(dict(current)) != entry["inserted_record"]:
                    raise ValueError("Batch row changed; rollback would lose changes")
                extra = conn.execute("SELECT 1 FROM source_events WHERE transaction_id=? AND id!=?", (ident, entry["source_event_id"])).fetchone()
                corrections = conn.execute("SELECT 1 FROM category_corrections WHERE transaction_id=?", (ident,)).fetchone()
                splits = conn.execute("SELECT 1 FROM transaction_allocations WHERE transaction_id=?", (ident,)).fetchone()
                if extra or corrections or splits:
                    raise ValueError("Later provenance/corrections require manual rollback")
            for entry in rows:
                conn.execute("DELETE FROM source_events WHERE id=?", (entry["source_event_id"],))
                conn.execute("DELETE FROM transactions WHERE id=?", (entry["transaction_id"],))
                conn.execute("UPDATE rehearsal_events SET status='rolled_back',transaction_id=NULL,source_event_id=NULL WHERE event_key=?", (entry["event_key"],))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Rollback referential integrity failure")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"rolled_back": len(rows), "batch_id": batch_id, "production_database_modified": False}
