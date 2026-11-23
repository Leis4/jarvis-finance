"""Allowlisted local observations; originals and authority never cross this boundary."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from jarvis.finance.review_backup import review_snapshot
from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_store import ReviewStore, encoded
from jarvis.knowledge.errors import KnowledgeUnavailable

POLICY = "local-observation-v1"
PURPOSE = "pending_observation_review"
CURRENCIES = frozenset({"RUB", "USD", "EUR", "GBP", "CNY", "JPY", "CHF", "HKD", "KZT", "BYN", "AMD", "AED"})
STATUSES = frozenset({"pending_review", "approve_new", "confirm_existing", "reject"})
MAX_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 10000


class ExportUnavailable(ValueError):
    """A fixed sanitized refusal; never carry source diagnostics."""
    def __init__(self, message: str, *, status: str = "unavailable") -> None:
        super().__init__(message)
        self.status = status


def _sha(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExportUnavailable("export_unavailable")
        result[key] = value
    return result


def _json(data: bytes) -> Any:
    if len(data) > MAX_BYTES:
        raise ExportUnavailable("export_unavailable")
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 200000:
            raise ExportUnavailable("export_unavailable")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    return value


def _money(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"-?\d{1,15}(?:\.\d{1,2})?", value):
        raise ExportUnavailable("export_unavailable")
    number = Decimal(value)
    if not number.is_finite():
        raise ExportUnavailable("export_unavailable")
    return format(number.quantize(Decimal("0.01")), "f")


def consume_engine_export(data: bytes, *, audience: str = "local_engine") -> dict[str, Any]:
    """Validate bounded JSON; this consumer cannot import, call tools or establish truth."""
    try:
        value = _json(data)
        if isinstance(value, dict) and value.get("format") == "jarvis-local-document-observations-v1":
            return _consume_documents(value, audience)
        keys = {"format", "policy", "purpose", "audience", "events", "apply_allowed", "financial_report_allowed"}
        if (audience != "local_engine" or not isinstance(value, dict) or set(value) != keys
                or value["format"] != "jarvis-local-observations-v1" or value["policy"] != POLICY
                or value["purpose"] != PURPOSE or value["audience"] != audience
                or value["apply_allowed"] is not False or value["financial_report_allowed"] is not False
                or not isinstance(value["events"], list) or not 0 < len(value["events"]) <= MAX_ITEMS):
            raise ExportUnavailable("export_unavailable")
        for index, event in enumerate(value["events"], 1):
            if (not isinstance(event, dict) or set(event) != {"ref", "date", "amount", "currency", "review_status", "revision"}
                    or event["ref"] != f"e{index}" or event["currency"] not in CURRENCIES
                    or event["review_status"] not in STATUSES or type(event["revision"]) is not int
                    or not 0 <= event["revision"] <= 1000000000
                    or not isinstance(event["date"], str) or len(event["date"]) != 10
                    or date.fromisoformat(event["date"]).isoformat() != event["date"]
                    or _money(event["amount"]) != event["amount"]):
                raise ExportUnavailable("export_unavailable")
        return {"status": "pending_review", "event_count": len(value["events"]),
                "income": None, "expenses": None, "free_cash": None,
                "coverage": "unverified", "apply_allowed": False, "database_modified": False}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, InvalidOperation):
        raise ExportUnavailable("export_unavailable") from None


def _consume_documents(value: dict[str, Any], audience: str) -> dict[str, Any]:
    if (audience != "local_engine" or set(value) != {"format", "policy", "purpose", "audience", "documents", "apply_allowed", "financial_report_allowed"}
            or value["policy"] != POLICY or value["purpose"] != PURPOSE or value["audience"] != audience
            or value["apply_allowed"] is not False or value["financial_report_allowed"] is not False
            or not isinstance(value["documents"], list) or not 0 < len(value["documents"]) <= MAX_ITEMS):
        raise ExportUnavailable("export_unavailable")
    for index, item in enumerate(value["documents"], 1):
        if (not isinstance(item, dict) or set(item) != {"ref", "page", "chunk_index", "review_status", "revision"}
                or item["ref"] != f"d{index}" or item["review_status"] not in STATUSES
                or type(item["page"]) is not int or not 1 <= item["page"] <= 200
                or type(item["chunk_index"]) is not int or not 0 <= item["chunk_index"] < MAX_ITEMS
                or type(item["revision"]) is not int or not 0 <= item["revision"] <= 1000000000):
            raise ExportUnavailable("export_unavailable")
    return {"status": "pending_review", "document_chunk_count": len(value["documents"]),
            "income": None, "expenses": None, "free_cash": None, "coverage": "unverified",
            "apply_allowed": False, "database_modified": False}


def _documents(snapshot: ReviewStore, packet: dict[str, Any], now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    from jarvis.finance.review_pdf_evidence import (
        PdfEvidenceUnavailable,
        assess_sources,
        parser_identity,
    )
    from jarvis.knowledge import loader
    if snapshot.metadata()["parser_version"] != parser_identity():
        raise ExportUnavailable("export_unavailable", status="parser_changed")
    items = snapshot.items()
    state, sources = assess_sources(packet, now, items=items)
    if state["status"] != "available":
        raise PdfEvidenceUnavailable(state["status"])
    if not 0 < len(items) <= MAX_ITEMS:
        raise ExportUnavailable("export_unavailable")
    documents, links = [], []
    for index, item in enumerate(items, 1):
        artifact = sources[item["source_sha256"]]
        chunk = item["original_record"]
        if (item["source_kind"] != "generated_pdf" or item["status"] == "reject"
                or item["operation"] != {"kind": "document_chunk"}
                or chunk not in artifact["chunks"] or item["pdf_index"] != chunk["chunk_index"]):
            raise ExportUnavailable("export_unavailable")
        documents.append({"ref": f"d{index}", "page": chunk["page"], "chunk_index": chunk["chunk_index"],
                          "review_status": item["status"], "revision": item["revision"]})
        links.append({"ref": f"d{index}", "item_hash": item["id"], "source_sha256": item["source_sha256"],
            "lineage_sha256": artifact["lineage_sha256"], "chunk_sha256": chunk["chunk_sha256"], "page": chunk["page"]})
    payload = {"format": "jarvis-local-document-observations-v1", "policy": POLICY, "purpose": PURPOSE,
        "audience": "local_engine", "documents": documents, "apply_allowed": False, "financial_report_allowed": False}
    consume_engine_export(encoded(payload).encode())
    return _wrap_projection(snapshot, now, payload, links, loader)


def _projection(snapshot: ReviewStore, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = snapshot.metadata()
    packet = _json(meta["packet"].encode("utf-8"))
    if packet.get("source_kind") == "generated_pdf":
        return _documents(snapshot, packet, now)
    from jarvis.finance.sber_intake import PARSER_VERSION
    if (packet.get("source_kind") != "sber_mcp" or meta["parser_version"] != PARSER_VERSION
            or packet.get("parser_version") != PARSER_VERSION):
        # PDF needs separately verified source artifacts; do not fall back to items alone.
        raise ExportUnavailable("export_unavailable")
    sources: dict[str, Any] = {}
    for source in packet.get("native_sources", []):
        original = source["original"]
        sha = source["sha256"]
        received = datetime.fromisoformat(original["received_at"])
        if (sha != _sha(original) or sha in sources or received.tzinfo is None
                or not timedelta(0) <= now - received <= timedelta(days=1)):
            raise ExportUnavailable("export_unavailable")
        sources[sha] = original
    items = snapshot.items()
    if not 0 < len(items) <= MAX_ITEMS:
        raise ExportUnavailable("export_unavailable")
    events = []
    links = []
    for index, item in enumerate(items, 1):
        if item["source_sha256"] not in sources or item.get("source_kind") != "sber_mcp":
            raise ExportUnavailable("export_unavailable")
        op = item["operation"]
        original = item["original_record"]
        source = sources[item["source_sha256"]]
        when = datetime.fromisoformat(original["date"])
        if (original not in source["response"]["operations"] or when.tzinfo is None
                or original.get("is_financial") is not True
                or original["id"] != op["external_id"] or when.date().isoformat() != op["tx_date"]
                or original["amount"]["currency"] != op["currency"]
                or _money(original["amount"]["amount"]) != _money(op["amount"])
                or source["request"]["resource"] != op["account"]):
            raise ExportUnavailable("export_unavailable")
        events.append({"ref": f"e{index}", "date": op["tx_date"], "amount": _money(op["amount"]),
                       "currency": op["currency"], "review_status": item["status"], "revision": item["revision"]})
        links.append({"ref": f"e{index}", "item_hash": item["id"], "source_sha256": item["source_sha256"]})
    payload = {"format": "jarvis-local-observations-v1", "policy": POLICY, "purpose": PURPOSE,
               "audience": "local_engine", "events": events, "apply_allowed": False, "financial_report_allowed": False}
    consume_engine_export(encoded(payload).encode())
    from jarvis.finance import sber_intake
    return _wrap_projection(snapshot, now, payload, links, sber_intake)


def _wrap_projection(snapshot: ReviewStore, now: datetime, payload: dict[str, Any], links: list[dict[str, Any]], parser: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = snapshot.metadata()
    binding = {"packet_sha256": meta["packet_hash"], "parser_version": meta["parser_version"],
               "parser_code_sha256": hashlib.sha256(Path(parser.__file__).read_bytes()).hexdigest(),
               "projection_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "review_audit_sha256": _sha(snapshot.audit()),
               "label_audit_sha256": _sha(HumanLabels(snapshot).audit()),
               "label_policy_sha256": _sha(HumanLabels(snapshot).policy())}
    manifest = {"format": "jarvis-private-projection-manifest-v1", "policy": POLICY,
                "purpose": PURPOSE, "binding": binding, "links": links,
                "derivative_sha256": _sha(payload), "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(), "apply_allowed": False}
    return payload, manifest


def build_engine_projection(store: ReviewStore, *, now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one verified snapshot; return distinct engine and operator-private artifacts."""
    from jarvis.finance.review_pdf_evidence import PdfEvidenceUnavailable
    try:
        clock = now or datetime.now(UTC)
        if clock.tzinfo is None or store.path.stat().st_size > 64 * 1024 * 1024:
            raise ExportUnavailable("export_unavailable")
        with review_snapshot(store) as snapshot:
            return _projection(snapshot, clock.astimezone(UTC))
    except ExportUnavailable:
        raise
    except PdfEvidenceUnavailable as error:
        raise ExportUnavailable("export_unavailable", status=error.status) from None
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, InvalidOperation, KnowledgeUnavailable):
        raise ExportUnavailable("export_unavailable") from None


def validate_current_projection(store: ReviewStore, payload: dict[str, Any], manifest: dict[str, Any], *, now: datetime | None = None) -> None:
    """Refuse stale or changed source/decision/label/parser; never reuse old good evidence."""
    try:
        clock = now or datetime.now(UTC)
        created, expiry = (datetime.fromisoformat(manifest[k]) for k in ("created_at", "expires_at"))
        current, binding = build_engine_projection(store, now=clock)
        if (created.tzinfo is None or expiry.tzinfo is None or not created <= clock <= expiry
                or expiry - created != timedelta(minutes=5) or manifest["policy"] != POLICY
                or manifest["format"] != "jarvis-private-projection-manifest-v1"
                or manifest["purpose"] != PURPOSE or manifest["apply_allowed"] is not False
                or current != payload or _sha(payload) != manifest["derivative_sha256"]
                or binding["binding"] != manifest["binding"] or binding["links"] != manifest["links"]):
            raise ExportUnavailable("export_unavailable")
    except (ValueError, TypeError, KeyError):
        raise ExportUnavailable("export_unavailable") from None


def write_engine_bundle(store: ReviewStore, destination: Path) -> dict[str, Any]:
    """Publish a new operator-private bundle atomically; never overwrite an old bundle."""
    temporary: Path | None = None
    try:
        if (destination.exists() or destination.is_symlink() or not destination.parent.is_dir()
                or destination.parent.is_symlink()):
            raise ExportUnavailable("export_unavailable")
        payload, manifest = build_engine_projection(store)
        temporary = Path(tempfile.mkdtemp(prefix=".projection-", dir=destination.parent))
        for name, value in (("engine.json", payload), ("manifest.private.json", manifest)):
            with (temporary / name).open("x", encoding="utf-8") as file:
                file.write(encoded(value))
                file.flush()
                os.fsync(file.fileno())
        # Serialize the last revision check/publication with review writers.
        # No records are written: closing the reserved transaction rolls it back.
        with store.connect() as lock:
            lock.execute("BEGIN IMMEDIATE")
            validate_current_projection(store, payload, manifest)
            os.rename(temporary, destination)
        temporary = None
        return {"status": "exported_local_only", "apply_allowed": False, "database_modified": False}
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError):
        raise ExportUnavailable("export_unavailable") from None
    finally:
        if temporary is not None:
            try:
                shutil.rmtree(temporary)
            except OSError:
                raise ExportUnavailable("export_unavailable") from None


def explain_document_reference(store: ReviewStore, payload: dict[str, Any], manifest: dict[str, Any], ref: str,
                               *, now: datetime | None = None) -> dict[str, Any]:
    """Explain a verified local document reference without text or financial authority."""
    validate_current_projection(store, payload, manifest, now=now)
    if payload.get("format") != "jarvis-local-document-observations-v1":
        raise ExportUnavailable("export_unavailable")
    document = next((d for d in payload["documents"] if d["ref"] == ref), None)
    link = next((d for d in manifest["links"] if d["ref"] == ref), None)
    if document is None or link is None:
        raise ExportUnavailable("export_unavailable")
    return {"ref": ref, "page": document["page"], "chunk_index": document["chunk_index"],
            "status": "verified_document_observation", "source_link_verified": True,
            "financial_report_allowed": False, "apply_allowed": False,
            "explanation": "Original/parser/page/chunk binding verified; financial meaning is unaccepted"}
