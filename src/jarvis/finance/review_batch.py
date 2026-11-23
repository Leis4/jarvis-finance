"""Explicit atomic review only for an identical immutable evidence group."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from jarvis.finance.review_store import ReviewStore, encoded


def _proof(item: dict[str, Any]) -> str:
    return encoded({k: v for k, v in item.items() if k not in {"pdf_index", "id", "revision", "decision", "status", "apply_allowed"}})


def group_preview(store: ReviewStore, item_id: str) -> dict[str, Any]:
    items = store.items()
    selected = next((i for i in items if i["id"] == item_id), None)
    if selected is None:
        raise ValueError("Unknown review item")
    same = [i for i in items if _proof(i) == _proof(selected)]
    evidence = {"proof_hash": hashlib.sha256(_proof(selected).encode()).hexdigest(),
                "revisions": {i["id"]: i["revision"] for i in same}}
    return {**evidence, "group_hash": hashlib.sha256(encoded(evidence).encode()).hexdigest(),
            "items": len(same), "eligible": len(same) > 1, "apply_allowed": False,
            "allowed_actions": ["confirm_existing", "reject", "revoke"]}


def decide_group(store: ReviewStore, item_id: str, *, group_hash: str, action: str,
                 actor: str, reason: str, confirmed: bool, candidate_id: int | None = None) -> dict[str, Any]:
    """Require a human digest confirmation; new-event bulk approval is forbidden."""
    if not confirmed or not actor.strip() or not reason.strip() or action not in {"confirm_existing", "reject", "revoke"}:
        raise ValueError("Explicit identical-group confirmation required; no bulk new-event approval")
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        events = store._audit(conn)
        preview = group_preview(store, item_id)
        if not preview["eligible"] or preview["group_hash"] != group_hash:
            raise ValueError("Group differs or stale; reload preview")
        latest = {e["item_id"]: e for e in events}
        originals = {r["id"]: json.loads(r["evidence"]) for r in conn.execute("SELECT * FROM review_items")}
        for ident in preview["revisions"]:
            if action == "confirm_existing" and candidate_id not in {c["db_id"] for c in originals[ident]["candidates"]}:
                raise ValueError("Evidence-backed candidate required for every event")
            if action != "confirm_existing" and candidate_id is not None:
                raise ValueError("Candidate only belongs to confirm_existing")
            if action == "revoke" and ident not in latest:
                raise ValueError("Nothing to revoke for one or more events")
        packet_hash = conn.execute("SELECT value FROM review_meta WHERE key='packet_hash'").fetchone()[0]
        tail = conn.execute("SELECT event_hash FROM review_events ORDER BY seq DESC LIMIT 1").fetchone()
        previous_hash = tail[0] if tail else ""
        for ident in preview["revisions"]:
            previous = latest.get(ident)
            event = {"seq": len(events) + 1, "item_id": ident, "action": action, "actor": actor.strip(),
                     "reason": reason.strip(), "candidate_id": candidate_id, "supersedes": previous["seq"] if previous else None,
                     "timestamp": datetime.now(UTC).isoformat(), "packet_hash": packet_hash, "group_hash": group_hash}
            payload = encoded(event)
            digest = hashlib.sha256((previous_hash + payload).encode()).hexdigest()
            conn.execute("INSERT INTO review_events VALUES (?,?,?,?)", (event["seq"], payload, previous_hash, digest))
            previous_hash = digest
            events.append(event)
        conn.commit()
    return {"group_hash": group_hash, "recorded": preview["items"], "database_modified": False, "apply_allowed": False}
