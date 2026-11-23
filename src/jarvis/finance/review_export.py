"""Export review evidence and decisions from one verified private snapshot."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from jarvis.finance.review_backup import review_snapshot
from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_sources import source_registry
from jarvis.finance.review_staging import build_staging_plan
from jarvis.finance.review_store import ReviewStore, encoded


def build_review_export(store: ReviewStore) -> dict[str, Any]:
    """Keep originals, revisions and both audit chains; never authorize apply."""
    with review_snapshot(store) as snapshot:
        meta = snapshot.metadata()
        labels = HumanLabels(snapshot)
        payload = {"format": "jarvis-private-review-export-v1", "packet_hash": meta["packet_hash"],
                   "parser_version": meta["parser_version"], "original_packet": json.loads(meta["packet"]),
                   "items": snapshot.items(), "review_events": snapshot.audit(),
                   "label_policy": labels.policy(), "label_events": labels.audit(),
                   "sources": source_registry(snapshot), "staging_plan": build_staging_plan(snapshot),
                   "apply_allowed": False, "database_modified": False,
                   "production_database_modified": False}
    return {"created_at": datetime.now(UTC).isoformat(), "payload": payload,
            "payload_sha256": hashlib.sha256(encoded(payload).encode()).hexdigest()}
