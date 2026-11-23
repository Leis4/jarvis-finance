"""Private review queue and append-only human decision journal."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source_ordinal(item: dict[str, Any]) -> int:
    """Preserve PDF ordinals while supporting explicitly typed native evidence."""
    return int(item["source_ordinal"]) if item.get("source_kind") == "sber_mcp" else int(item["pdf_index"]) + 1


def _item_key(item: dict[str, Any]) -> str:
    if item.get("native_review_key") is not None:
        key = item["native_review_key"]
        if not isinstance(key, str) or len(key) != 24 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid native review identity")
        return key
    index = source_ordinal(item) - 1
    return hashlib.sha256(f"{item['source_sha256']}:{index}".encode()).hexdigest()[:24]


class ReviewStore:
    """Keep review decisions separate from bank and production databases."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.path = self.workspace / "review.sqlite"

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file() or self.path.is_symlink():
            raise ValueError("Initialize a private review workspace first")
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Import packet once; never silently replace reviewed evidence."""
        if packet.get("apply_allowed") is not False or not isinstance(packet.get("items"), list):
            raise ValueError("An immutable triage packet with apply_allowed=false is required")
        content = encoded(packet)
        digest = hashlib.sha256(content.encode()).hexdigest()
        parser_version: str
        if packet.get("source_kind") == "sber_mcp":
            from jarvis.finance.sber_intake import PARSER_VERSION
            if packet.get("parser_version") != PARSER_VERSION or any(i.get("source_kind") != "sber_mcp" for i in packet["items"]):
                raise ValueError("Native source/parser identity required")
            parser_version = PARSER_VERSION
        elif packet.get("source_kind") == "generated_pdf":
            from jarvis.finance.review_pdf_evidence import (
                parser_identity,
                verified_sources,
            )
            verified_sources(packet, datetime.now(UTC))
            parser_version = parser_identity()
        elif packet.get("source_kind", "pdf") == "pdf":
            from jarvis.finance.bank import sber_statement
            parser_version = "sber-pdf:" + hashlib.sha256(Path(sber_statement.__file__).read_bytes()).hexdigest()
        else:
            raise ValueError("Unsupported review source kind")
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Review database cannot be a symlink")
        conn = sqlite3.connect(self.path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "review_meta" not in tables:
                raise ValueError("Not a review database; use an empty private workspace")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS review_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS review_items (id TEXT PRIMARY KEY,evidence TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS review_events (seq INTEGER PRIMARY KEY,payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            """)
            old = conn.execute("SELECT value FROM review_meta WHERE key='packet_hash'").fetchone()
            if old and old[0] != digest:
                raise ValueError("Packet changed; use a new workspace, preserve old decisions")
            if not old:
                conn.executemany("INSERT INTO review_meta VALUES (?,?)", [
                    ("packet_hash", digest), ("packet", content), ("parser_version", parser_version),
                ])
                for item in packet["items"]:
                    key = _item_key(item)
                    conn.execute("INSERT INTO review_items VALUES (?,?)", (key, encoded(item)))
                conn.commit()
        finally:
            conn.close()
        return {"packet_hash": digest, "count": len(packet["items"]), "production_database_modified": False}

    def metadata(self) -> dict[str, str]:
        with self.connect() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM review_meta")}

    def claim_chat(self, conversation: str, request: str, payload: dict[str, Any]) -> bool:
        """Claim a private turn once, without changing financial events."""
        digest = hashlib.sha256(encoded(payload).encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS chat_turns(request TEXT PRIMARY KEY,conversation TEXT NOT NULL,created_at TEXT NOT NULL,request_hash TEXT NOT NULL,message TEXT,answer TEXT)")
            conn.execute("UPDATE chat_turns SET message=NULL,answer=NULL WHERE created_at<?", ((datetime.now(UTC) - timedelta(days=7)).isoformat(),))
            prior = conn.execute("SELECT conversation,request_hash FROM chat_turns WHERE request=?", (request,)).fetchone()
            if prior:
                if tuple(prior) != (conversation, digest):
                    raise ValueError("Conflicting conversation request identity")
                conn.commit()
                return False
            conn.execute("INSERT INTO chat_turns VALUES (?,?,?,?,?,NULL)", (request, conversation, datetime.now(UTC).isoformat(), digest, payload["message"]))
            conn.commit()
            return True

    def finish_chat(self, request: str, answer: dict[str, Any]) -> None:
        """Complete a claimed turn once; crashed pending turns are never resent."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("UPDATE chat_turns SET answer=? WHERE request=? AND answer IS NULL AND message IS NOT NULL", (encoded(answer), request)).rowcount != 1:
                raise ValueError("Conversation turn cannot be completed")
            conn.commit()

    def chat_history(self, conversation: str) -> list[dict[str, Any]]:
        """Read historical turns, expiring private content after seven days."""
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='chat_turns'").fetchone():
                return []
            conn.execute("UPDATE chat_turns SET message=NULL,answer=NULL WHERE created_at<?", ((datetime.now(UTC) - timedelta(days=7)).isoformat(),))
            conn.commit()
            rows = conn.execute("SELECT request,created_at,message,answer FROM chat_turns WHERE conversation=? ORDER BY created_at DESC,request DESC LIMIT 50", (conversation,)).fetchall()
            return [{"request_id": r[0], "created_at": r[1], "message": r[2],
                     "answer": {**json.loads(r[3]), "current": False} if r[3] else None,
                     "historical": True} for r in reversed(rows)]

    def append_native_capture(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Atomically retain native evidence without duplicating reviewed observations."""
        from jarvis.finance.sber_intake import digest, merge_native_captures

        if not self.path.exists():
            normalized = merge_native_captures([packet])
            result = self.initialize(normalized)
            return {**result, "capture_count": 1, "database_modified": False, "apply_allowed": False}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events = self._audit(conn)
            meta = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM review_meta")}
            old = json.loads(meta["packet"])
            if old.get("source_kind") != "sber_mcp":
                raise ValueError("Append only to a native review workspace")
            retained = [entry["packet"] for entry in old.get("capture_packets", [])] or [old]
            sha = digest(packet)
            if sha in {digest(c) for c in retained}:
                return {"count": len(old["items"]), "capture_count": len(retained),
                        "database_modified": False, "apply_allowed": False}
            merged = merge_native_captures([*retained, packet])
            content = encoded(merged)
            packet_hash = hashlib.sha256(content.encode()).hexdigest()
            for item in merged["items"]:
                conn.execute("INSERT OR REPLACE INTO review_items VALUES (?,?)", (_item_key(item), encoded(item)))
            conn.execute("UPDATE review_meta SET value=? WHERE key='packet'", (content,))
            conn.execute("UPDATE review_meta SET value=? WHERE key='packet_hash'", (packet_hash,))
            self._append_event(conn, {"seq": len(events) + 1, "item_id": "native_capture:" + sha,
                "domain": "native_capture", "action": "observe_capture", "source_sha256": sha,
                "timestamp": datetime.now(UTC).isoformat(), "packet_hash": packet_hash})
            conn.commit()
        return {"count": len(merged["items"]), "capture_count": merged["capture_count"],
                "database_modified": False, "apply_allowed": False}

    @staticmethod
    def _audit(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM review_meta")}
        if hashlib.sha256(meta["packet"].encode()).hexdigest() != meta["packet_hash"]:
            raise ValueError("Review packet integrity failure")
        packet = json.loads(meta["packet"])
        if "capture_packets" in packet:
            from jarvis.finance.sber_intake import digest, merge_native_captures
            captures = packet["capture_packets"]
            if any(digest(c["packet"]) != c["sha256"] for c in captures):
                raise ValueError("Native capture integrity failure")
            if merge_native_captures([c["packet"] for c in captures]) != packet:
                raise ValueError("Native aggregate integrity failure")
        expected_items = {_item_key(i): encoded(i)
                          for i in packet["items"]}
        actual_items = {r["id"]: r["evidence"] for r in conn.execute("SELECT * FROM review_items")}
        if expected_items != actual_items:
            raise ValueError("Review evidence integrity failure")
        for key, value in meta.items():
            if key.startswith("memory_note:") and hashlib.sha256(value.encode()).hexdigest() != key.split(":", 1)[1]:
                raise ValueError("Memory note integrity failure")
            if key.startswith("financial_fact:") and hashlib.sha256(value.encode()).hexdigest() != key.split(":", 1)[1]:
                raise ValueError("Financial fact integrity failure")
        previous = ""
        events: list[dict[str, Any]] = []
        for row in conn.execute("SELECT * FROM review_events ORDER BY seq"):
            expected = hashlib.sha256((previous + row["payload"]).encode()).hexdigest()
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                raise ValueError("Review audit integrity failure")
            event = json.loads(row["payload"])
            if event["seq"] != row["seq"] or row["seq"] != len(events) + 1:
                raise ValueError("Review audit sequence failure")
            if str(event.get("item_id", "")).startswith("financial_fact:"):
                fact = json.loads(meta[event["item_id"]])
                if (event.get("domain") != "financial_fact" or event.get("kind") != fact["kind"] or
                        event.get("source_sha256") != fact["source_sha256"] or
                        event.get("action") not in {"accept_financial_fact", "revoke_financial_fact"}):
                    raise ValueError("Financial decision binding failure")
            if event.get("domain") == "native_capture":
                sha = event.get("source_sha256")
                if (event.get("action") != "observe_capture" or event.get("item_id") != "native_capture:" + str(sha)
                        or sha not in {c["sha256"] for c in packet.get("capture_packets", [])}):
                    raise ValueError("Native capture audit binding failure")
            if event.get("domain") in {"statement_source", "statement_projection"}:
                key = str(event.get("item_id", ""))
                raw = meta.get(key, "")
                if event["domain"] == "statement_source":
                    if (not key.startswith("statement:") or hashlib.sha256(raw.encode()).hexdigest() != key[10:]
                            or json.loads(raw).get("sha256") != event.get("source_sha256")
                            or event.get("action") not in {"accept_statement", "revoke_statement"}):
                        raise ValueError("Statement source audit binding failure")
                elif (not key.startswith("statement_projection:") or event.get("action") != "project_statements"
                        or hashlib.sha256(raw.encode()).hexdigest() != event.get("metadata_sha256")):
                    raise ValueError("Statement projection audit binding failure")
            if event.get("domain") == "memory_note":
                note = json.loads(meta.get(event["item_id"], "{}"))
                if (not str(event["item_id"]).startswith("memory_note:") or event.get("kind") != note.get("kind")
                        or event.get("action") not in {"record_note", "revoke_note"}):
                    raise ValueError("Memory note audit binding failure")
            if event.get("domain") == "private_teacher":
                action, key = event.get("action"), str(event.get("item_id", ""))
                if action in {"grant_teacher", "propose_category"}:
                    expected_prefix = "teacher_policy:" if action == "grant_teacher" else "teacher_result:"
                    hash_field = "policy_sha256" if action == "grant_teacher" else "result_sha256"
                    if (not key.startswith(expected_prefix) or key not in meta
                            or hashlib.sha256(meta[key].encode()).hexdigest() != event.get(hash_field)):
                        raise PermissionError("Teacher evidence integrity refused")
                elif action == "revoke_teacher":
                    if key not in meta or not key.startswith("teacher_policy:"):
                        raise PermissionError("Teacher revoke binding refused")
                elif action == "attempt_teacher":
                    if key != "teacher_request:" + str(event.get("request_sha256")) or "teacher_policy:" + str(event.get("permission_id")) not in meta:
                        raise PermissionError("Teacher attempt binding refused")
                else:
                    raise PermissionError("Unknown teacher audit action")
            if event.get('domain') == 'annotation':
                key = str(event.get('item_id', ''))
                raw = meta.get(key, '')
                if (event.get('action') != 'import_ai_proposals' or not key.startswith('annotation_result:')
                        or hashlib.sha256(raw.encode()).hexdigest() != key.removeprefix('annotation_result:')
                        or event.get('payload_sha256') != key.removeprefix('annotation_result:')):
                    raise ValueError('Annotation evidence integrity failure')
            events.append(event)
            previous = expected
        if {key for key in meta if key.startswith('annotation_result:')} != {
                e['item_id'] for e in events if e.get('domain') == 'annotation'}:
            raise ValueError('Annotation evidence audit missing')
        groups = {event['item_id']: event for event in events if event.get('domain') == 'statement_group'}
        if {key.removeprefix('statement_group:') for key in meta if key.startswith('statement_group:')} != set(groups):
            raise ValueError('Statement group receipt missing audit binding')
        for ident, event in groups.items():
            receipt = meta.get('statement_group:' + ident, '')
            if (event.get('action') not in {'start_group', 'finish_group'}
                    or hashlib.sha256(receipt.encode()).hexdigest() != event.get('receipt_sha256')
                    or (event['action'] == 'finish_group' and encoded(event.get('result')) != receipt)):
                raise ValueError('Statement group receipt integrity failure')
        return events

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return self._audit(conn)

    @staticmethod
    def _append_event(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
        """Append to the shared decision chain under the caller's write lock."""
        payload = encoded(event)
        last = conn.execute("SELECT event_hash FROM review_events ORDER BY seq DESC LIMIT 1").fetchone()
        previous_hash = last[0] if last else ""
        digest = hashlib.sha256((previous_hash + payload).encode()).hexdigest()
        conn.execute("INSERT INTO review_events VALUES (?,?,?,?)", (event["seq"], payload, previous_hash, digest))

    def register_financial_fact(self, kind: str, source: Path) -> str:
        """Freeze a source document without accepting any financial assertion."""
        if kind not in {"inventory", "balances", "obligations", "reserve"}:
            raise ValueError("Unknown financial fact kind")
        path = source.resolve()
        if source.is_symlink() or not path.is_relative_to(self.workspace) or path.suffix != ".json":
            raise ValueError("Financial source must be an original JSON in the private workspace")
        raw = path.read_bytes()
        original = json.loads(raw)
        if not isinstance(original, dict) or original.get("schema") != "financial-evidence-v1" or original.get("kind") != kind:
            raise ValueError("Financial evidence schema/kind mismatch")
        fact = {"kind": kind, "source_path": str(path), "source_root": str(self.workspace), "original": original,
                "source_sha256": hashlib.sha256(raw).hexdigest(), "parser_version": "financial-json-v1"}
        value = encoded(fact)
        ident = hashlib.sha256(value.encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._audit(conn)
            if path.read_bytes() != raw:
                raise ValueError("Source changed while registering evidence")
            conn.execute("INSERT OR IGNORE INTO review_meta VALUES (?,?)", ("financial_fact:" + ident, value))
            conn.commit()
        return ident

    def record_memory_note(self, kind: str, text: str, *, actor: str, model_version: str | None = None,
                           expires_at: datetime | None = None, now: datetime | None = None) -> str:
        """Persist nonfinancial notes; no note can establish a fact or category label."""
        current = now or datetime.now(UTC)
        if (kind not in {"preference", "hypothesis", "prediction"} or not 1 <= len(text.strip()) <= 500
                or not 1 <= len(actor.strip()) <= 100 or current.tzinfo is None
                or (model_version is not None and not 1 <= len(model_version.strip()) <= 100)
                or (kind == "prediction" and (not model_version or expires_at is None))
                or (expires_at is not None and (expires_at.tzinfo is None or not current < expires_at <= current + timedelta(days=365)))):
            raise ValueError("Typed bounded nonfinancial memory note required")
        note = {"kind": kind, "text": text.strip(), "actor": actor.strip(), "model_version": model_version,
                "created_at": current.isoformat(), "expires_at": expires_at.isoformat() if expires_at else None,
                "fact_authority": False, "apply_allowed": False}
        value = encoded(note)
        ident = hashlib.sha256(value.encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events = self._audit(conn)
            key = "memory_note:" + ident
            if not conn.execute("SELECT 1 FROM review_meta WHERE key=?", (key,)).fetchone():
                conn.execute("INSERT INTO review_meta VALUES (?,?)", (key, value))
                self._append_event(conn, {"seq": len(events) + 1, "domain": "memory_note", "item_id": key,
                    "kind": kind, "action": "record_note", "actor": actor.strip(), "timestamp": current.isoformat()})
                conn.commit()
        return ident

    def revoke_memory_note(self, ident: str, *, actor: str, expected_revision: int) -> None:
        """Revoke a note explicitly while retaining its immutable content and journal."""
        if not actor.strip():
            raise ValueError("Explicit memory actor required")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events = self._audit(conn)
            key = "memory_note:" + ident
            previous = next((e for e in reversed(events) if e["item_id"] == key), None)
            if previous is None or previous["seq"] != expected_revision or previous["action"] == "revoke_note":
                raise ValueError("Stale or unknown memory note")
            self._append_event(conn, {"seq": len(events) + 1, "domain": "memory_note", "item_id": key,
                "kind": previous["kind"], "action": "revoke_note", "actor": actor.strip(),
                "supersedes": previous["seq"], "timestamp": datetime.now(UTC).isoformat()})
            conn.commit()

    def financial_fact(self, ident: str) -> dict[str, Any]:
        """Revalidate the original and separate latest human acceptance."""
        events = self.audit()
        value = self.metadata().get("financial_fact:" + ident)
        if value is None:
            raise ValueError("Financial fact unavailable")
        fact: dict[str, Any] = json.loads(value)
        path = Path(fact["source_path"])
        if path.is_symlink() or not path.resolve().is_relative_to(Path(fact["source_root"])):
            raise ValueError("Financial source escaped its registered root")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != fact["source_sha256"] or json.loads(raw) != fact["original"]:
            raise ValueError("Financial original changed")
        decision = next((e for e in reversed(events) if e["item_id"] == "financial_fact:" + ident), None)
        return {**fact, "fact_id": ident, "decision": decision}

    def decide_financial_fact(self, ident: str, action: str, *, actor: str, reason: str,
                              expected_revision: int) -> dict[str, Any]:
        """Accept or revoke a named financial fact, never an operation or label."""
        if action not in {"accept_financial_fact", "revoke_financial_fact"} or not actor.strip() or not reason.strip():
            raise ValueError("Explicit separate human financial-fact action required")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events = self._audit(conn)
            if action == "accept_financial_fact":
                fact = self.financial_fact(ident)
            else:
                value = self.metadata().get("financial_fact:" + ident)
                if value is None:
                    raise ValueError("Unknown financial fact")
                fact = json.loads(value)
            previous = next((e for e in reversed(events) if e["item_id"] == "financial_fact:" + ident), None)
            if (previous["seq"] if previous else 0) != expected_revision:
                raise ValueError("Stale financial fact revision")
            if action == "revoke_financial_fact" and previous is None:
                raise ValueError("Nothing to revoke")
            event = {"seq": len(events) + 1, "item_id": "financial_fact:" + ident, "domain": "financial_fact",
                     "kind": fact["kind"], "action": action, "actor": actor.strip(), "reason": reason.strip(),
                     "source_sha256": fact["source_sha256"], "timestamp": datetime.now(UTC).isoformat(),
                     "supersedes": previous["seq"] if previous else None, "packet_hash": self.metadata()["packet_hash"]}
            self._append_event(conn, event)
            conn.commit()
            return {**event, "production_database_modified": False, "apply_allowed": False}

    def items(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            events = self._audit(conn)
            latest = {e["item_id"]: e for e in events}
            result = []
            for row in conn.execute("SELECT * FROM review_items ORDER BY id"):
                item = json.loads(row["evidence"])
                event = latest.get(row["id"])
                result.append({**item, "id": row["id"], "revision": event["seq"] if event else 0,
                               "decision": event, "status": event["action"] if event and event["action"] != "revoke" else "pending_review",
                               "apply_allowed": False})
                if item.get("conflict_reasons"):
                    result[-1]["status"] = "pending_review"
            return result

    def decide(self, item_id: str, action: str, *, actor: str, reason: str,
               expected_revision: int, candidate_id: int | None = None) -> dict[str, Any]:
        """Record explicit human intent; revocation preserves all audit history."""
        if action not in {"confirm_existing", "approve_new", "reject", "revoke"} or not actor.strip() or not reason.strip():
            raise ValueError("Explicit action, actor and evidence reason are required")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events = self._audit(conn)
            evidence = conn.execute("SELECT evidence FROM review_items WHERE id=?", (item_id,)).fetchone()
            if evidence is None:
                raise ValueError("Unknown review item")
            previous = next((e for e in reversed(events) if e["item_id"] == item_id), None)
            if (previous["seq"] if previous else 0) != expected_revision:
                raise ValueError("Stale review revision; reload evidence")
            item = json.loads(evidence[0])
            if item.get("conflict_reasons") and action in {"approve_new", "confirm_existing"}:
                raise ValueError("Native source conflict requires evidence review")
            if action == "confirm_existing" and candidate_id not in {c["db_id"] for c in item["candidates"]}:
                raise ValueError("Select an evidence-backed candidate")
            if action != "confirm_existing" and candidate_id is not None:
                raise ValueError("Candidate ID only belongs to confirm_existing")
            if action == "revoke" and previous is None:
                raise ValueError("Nothing to revoke")
            event = {"seq": len(events) + 1, "item_id": item_id, "action": action,
                     "actor": actor.strip(), "reason": reason.strip(), "candidate_id": candidate_id,
                     "supersedes": previous["seq"] if previous else None,
                     "timestamp": datetime.now(UTC).isoformat(), "packet_hash": self.metadata()["packet_hash"]}
            self._append_event(conn, event)
            conn.commit()
            return {**event, "production_database_modified": False, "apply_allowed": False}
