"""Hash-linked lifecycle evidence; no cross-backend ACID claim."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.knowledge.errors import KnowledgeUnavailable


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class IndexJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self.locked() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS lifecycle (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL, "
                         "previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL)")

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers across processes, keeping durable intent before backend calls."""
        guard = sqlite3.connect(str(self.path) + ".lock", timeout=5)
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            guard.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        finally:
            conn.close()
            guard.close()

    def states(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        previous = ""
        for expected, (seq, encoded, prior, hashed) in enumerate(conn.execute(
                "SELECT seq,payload,previous_hash,event_hash FROM lifecycle ORDER BY seq"), 1):
            payload = json.loads(encoded)
            if seq != expected or seq != payload.get("seq") or prior != previous or hashed != digest([prior, payload]):
                raise KnowledgeUnavailable("Knowledge lifecycle integrity failed")
            if payload["state"] not in {"intent", "successful", "failed", "uncertain", "revoked"}:
                raise KnowledgeUnavailable("Unknown lifecycle state")
            states[payload["doc_id"]] = payload
            previous = hashed
        return states

    def append(self, conn: sqlite3.Connection, doc_id: str, operation: str, state: str, descriptor: dict[str, Any]) -> None:
        self.states(conn)
        last = conn.execute("SELECT seq,event_hash FROM lifecycle ORDER BY seq DESC LIMIT 1").fetchone()
        seq, previous = (last[0] + 1, last[1]) if last else (1, "")
        payload = {"seq": seq, "doc_id": doc_id, "operation": operation, "state": state,
                   "descriptor": descriptor, "at": datetime.now(UTC).isoformat()}
        conn.execute("INSERT INTO lifecycle VALUES (?,?,?,?)", (seq, json.dumps(payload, sort_keys=True), previous,
                                                               digest([previous, payload])))
        # Durable intent, while a separate SQLite writer guard remains locked.
        conn.commit()
