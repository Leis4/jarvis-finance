"""Explicit human category labels with frozen policy and independent split."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from jarvis.finance.classification.evaluation import (
    Example,
    independent_split,
    normalize,
)
from jarvis.finance.review_store import ReviewStore, encoded, source_ordinal


class HumanLabels:
    def __init__(self, store: ReviewStore):
        self.store = store

    def configure(self, policy: dict[str, Any]) -> None:
        """Freeze taxonomy/cutoff before collection; never retune on holdout."""
        date.fromisoformat(policy["cutoff"])
        if (not policy["version"] or not policy["instructions"] or policy["seed"] != 42
                or policy["min_train"] < 1 or policy["min_test"] < 1
                or not policy["categories"] or len(set(policy["categories"])) != len(policy["categories"])
                or any(not c.strip() or c in {"unknown", "uncategorized"} for c in policy["categories"])):
            raise ValueError("Valid frozen label policy required")
        self.store.audit()
        payload = encoded(policy)
        with self.store.connect() as conn:
            old = conn.execute("SELECT value FROM review_meta WHERE key='label_policy'").fetchone()
            if old and old[0] != payload:
                raise ValueError("Label policy/cutoff frozen; use a new versioned workspace")
            if not old:
                conn.executemany("INSERT INTO review_meta VALUES (?,?)", [("label_policy", payload),
                                  ("label_policy_hash", hashlib.sha256(payload.encode()).hexdigest())])
                conn.execute("CREATE TABLE label_events(seq INTEGER PRIMARY KEY,payload TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL)")
                conn.commit()

    def policy(self) -> dict[str, Any] | None:
        meta = self.store.metadata()
        if "label_policy" not in meta:
            return None
        if hashlib.sha256(meta["label_policy"].encode()).hexdigest() != meta["label_policy_hash"]:
            raise ValueError("Label policy integrity failure")
        return dict(json.loads(meta["label_policy"]))

    def audit(self, _connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        self.store.audit()
        if self.policy() is None:
            return []
        previous = ""
        events: list[dict[str, Any]] = []
        with self.store.connect() if _connection is None else nullcontext(_connection) as conn:
            for row in conn.execute("SELECT * FROM label_events ORDER BY seq"):
                digest = hashlib.sha256((previous + row["payload"]).encode()).hexdigest()
                event = json.loads(row["payload"])
                if row["previous_hash"] != previous or row["event_hash"] != digest or event["seq"] != len(events) + 1 or row["seq"] != event["seq"]:
                    raise ValueError("Label audit integrity failure")
                events.append(event)
                previous = digest
        return events

    def latest(self) -> dict[str, dict[str, Any]]:
        return {e["item_id"]: e for e in self.audit()}

    def decide(self, item_id: str, action: str, *, actor: str, reason: str,
               expected_revision: int, category: str | None = None,
               transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
               _connection: sqlite3.Connection | None = None, _group_hash: str | None = None) -> dict[str, Any]:
        """Confirm/reject/revoke category separately from financial identity."""
        if json.loads(self.store.metadata()["packet"]).get("source_kind") == "generated_pdf":
            raise ValueError("Document observations are not financial category labels")
        policy = self.policy()
        if policy is None or action not in {"confirm", "reject", "revoke"} or not actor.strip() or not reason.strip():
            raise ValueError("Frozen policy and explicit human decision required")
        if (action == "confirm" and category not in policy["categories"]) or (action != "confirm" and category is not None):
            raise ValueError("Category must follow policy and explicit confirmation")
        item = next((i for i in self.store.items() if i["id"] == item_id), None)
        if item is None:
            raise ValueError("Unknown label item")
        if item.get("conflict_reasons") and action == "confirm":
            raise ValueError("Native source conflict cannot produce a category label")
        with self.store.connect() if _connection is None else nullcontext(_connection) as conn:
            if _connection is None:
                conn.execute("BEGIN IMMEDIATE")
            elif not conn.in_transaction:
                raise ValueError("Group decision requires an active transaction")
            if transaction_guard is not None:
                transaction_guard(conn)
            # Audit reads are safe under this reserved write lock; concurrent writes wait.
            events = self.audit(conn)
            old = next((e for e in reversed(events) if e["item_id"] == item_id), None)
            if (old["seq"] if old else 0) != expected_revision:
                raise ValueError("Stale label revision")
            if action == "revoke" and old is None:
                raise ValueError("No category decision to revoke")
            event = {"seq": len(events) + 1, "item_id": item_id, "action": action, "category": category,
                     "actor": actor.strip(), "reason": reason.strip(), "timestamp": datetime.now(UTC).isoformat(),
                     "source_sha256": item["source_sha256"], "source_ordinal": source_ordinal(item),
                     "policy_hash": self.store.metadata()["label_policy_hash"], "supersedes": old["seq"] if old else None}
            if _group_hash is not None:
                event["group_hash"] = _group_hash
            payload = encoded(event)
            previous = conn.execute("SELECT event_hash FROM label_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = previous[0] if previous else ""
            conn.execute("INSERT INTO label_events VALUES (?,?,?,?)", (event["seq"], payload, prev_hash,
                         hashlib.sha256((prev_hash + payload).encode()).hexdigest()))
            if _connection is None:
                conn.commit()
        return {**event, "production_database_modified": False}

    def export(self) -> dict[str, Any]:
        """Export confirmed-only examples; gate independent evaluation, never train."""
        policy = self.policy()
        if policy is None:
            return {"status": "blocked", "reason": "Frozen label policy required", "metrics": None}
        items = {i["id"]: i for i in self.store.items()}
        labels = [e for e in self.latest().values() if e["action"] == "confirm"]
        suspended = [e for e in labels if items[e["item_id"]].get("conflict_reasons")]
        labels = [e for e in labels if not items[e["item_id"]].get("conflict_reasons")]
        examples = []
        for label in labels:
            row = items[label["item_id"]]["operation"]
            examples.append(Example(int(label["item_id"][:12], 16), date.fromisoformat(row["tx_date"]),
                                    normalize(row["merchant"]), normalize(row["merchant"] + " " + row["description"]), label["category"]))
        if len({e.transaction_id for e in examples}) != len(examples):
            raise ValueError("Stable label ID collision")
        train, test, exclusions = independent_split(examples, date.fromisoformat(policy["cutoff"]))
        ready = len(train) >= policy["min_train"] and len(test) >= policy["min_test"] and len({e.label for e in train}) >= 2
        return {"status": "ready_for_independent_evaluation" if ready else "blocked",
                "reason": None if ready else "Insufficient independent confirmed labels; no training",
                "label_source": "explicit_review_human_category", "human_labels": len(labels), "policy": policy,
                "suspended_source_conflict_labels": len(suspended), "suspended_label_audit": suspended,
                "policy_hash": self.store.metadata()["label_policy_hash"], "seed": 42, "metrics": None,
                "split": "time_and_merchant_disjoint", "train_size": len(train), "test_size": len(test),
                "class_counts": dict(Counter(e.label for e in examples)), "exclusions": exclusions,
                "train": [dict(asdict(e), day=e.day.isoformat()) for e in train],
                "holdout": [dict(asdict(e), day=e.day.isoformat()) for e in test],
                "label_audit": labels, "data_sha256": hashlib.sha256(encoded(labels).encode()).hexdigest(),
                "production_database_modified": False}

    def progress(self) -> dict[str, Any]:
        """Show actual confirmations and a bounded representative review shortlist."""
        from jarvis.finance.review_backup import review_snapshot
        with review_snapshot(self.store) as frozen:
            labels = HumanLabels(frozen)
            items, latest = frozen.items(), labels.latest()
            by_id = {i["id"]: i for i in items}
            confirmed = {e["item_id"]: e for e in latest.values() if e["action"] == "confirm"
                and not by_id[e["item_id"]].get("conflict_reasons") and by_id[e["item_id"]]["status"] != "reject"}
            contexts: dict[tuple[str, ...], set[str]] = {}
            for ident, event in confirmed.items():
                row = by_id[ident]["operation"]
                try:
                    negative = Decimal(str(row["amount"])) < 0
                except (KeyError, InvalidOperation):
                    continue
                context = tuple(" ".join(str(row.get(k) or "").casefold().split()) for k in ("merchant", "description", "currency")) + (str(negative),)
                contexts.setdefault(context, set()).add(event["category"])
            pending = [i for i in items if i["id"] not in confirmed and i["status"] != "reject"]
            eligible = [i for i in pending if not i.get("conflict_reasons")]
            selected, seen = [], set()
            for item in eligible:
                merchant = " ".join(str(item["operation"].get("merchant") or "").casefold().split()) or item["id"]
                if merchant not in seen:
                    selected.append(item["id"])
                    seen.add(merchant)
            selected += [i["id"] for i in eligible if i["id"] not in selected]
            return {"status": "available" if labels.policy() else "blocked", "human_confirmed": len(confirmed),
                "pending_categories": len(pending), "conflicting_label_contexts": sum(len(v) > 1 for v in contexts.values()),
                "source_conflicts": sum(bool(i.get("conflict_reasons")) for i in items), "review_ids": selected[:20],
                "label_revision": max((e["seq"] for e in latest.values()), default=0),
                "policy_configured": labels.policy() is not None, "proposals_are_labels": False,
                "training_allowed": False, "production_database_modified": False}

    def memory(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Derive typed private memory from one source/decision/label snapshot."""
        from jarvis.finance.review_backup import review_snapshot
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("Aware memory clock required")
        with review_snapshot(self.store) as frozen:
            labels = HumanLabels(frozen)
            audit, meta, items = labels.audit(), frozen.metadata(), frozen.items()
            policy = labels.policy()
            by_id = {i["id"]: i for i in items}
            latest = {e["item_id"]: e for e in audit}
            facts = [{"kind": "human_confirmed_category", "item_id": e["item_id"], "category": e["category"],
                "confirmed_by": e["actor"], "confirmed_at": e["timestamp"], "policy_version": policy["version"] if policy else None,
                "source_sha256": e["source_sha256"], "revision": e["seq"], "financial_source_approved": False}
                for e in latest.values() if e["action"] == "confirm" and not by_id[e["item_id"]].get("conflict_reasons")]
            notes: dict[str, list[dict[str, Any]]] = {k: [] for k in ("preference", "hypothesis", "prediction")}
            events = frozen.audit()
            for key, value in meta.items():
                if not key.startswith("memory_note:"):
                    continue
                note = json.loads(value)
                decision = next(e for e in reversed(events) if e["item_id"] == key)
                expired = bool(note["expires_at"] and datetime.fromisoformat(note["expires_at"]) <= current)
                state = "revoked" if decision["action"] == "revoke_note" else "expired" if expired else "nonfinancial_note"
                notes[note["kind"]].append({**note, "id": key.split(":", 1)[1], "revision": decision["seq"], "status": state})
            binding = {"packet_hash": meta["packet_hash"], "policy": policy, "label_audit": audit, "review_audit": events}
            return {"status": "read_only_memory", "facts": facts, "preferences": notes["preference"],
                "hypotheses": notes["hypothesis"], "predictions": notes["prediction"], "category_history": audit,
                "snapshot_sha256": hashlib.sha256(encoded(binding).encode()).hexdigest(), "checked_at": current.isoformat(),
                "financial_apply_allowed": False, "database_modified": False,
                "limitations": ["Category confirmation is not financial source acceptance", "Notes and model confidence never become labels or financial facts"]}
