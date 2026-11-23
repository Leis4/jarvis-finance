"""Generated PDF observations; never financial facts or arbitrary original paths."""
from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.knowledge import loader
from jarvis.knowledge.errors import KnowledgeParseError, KnowledgeUnavailable
from jarvis.knowledge.pdf_process import PdfWorkerUnavailable, process_pdf
from jarvis.knowledge.store import KnowledgeStore

if TYPE_CHECKING:
    from jarvis.finance.review_store import ReviewStore

CONTRACT = {"policy": "generated-pdf-observation-v1", "purpose": "document_review",
            "financial_source_approved": False, "external_transmission_allowed": False}


class PdfEvidenceUnavailable(ValueError):
    """Fixed lifecycle status, without document bytes, paths or exception payloads."""
    def __init__(self, status: str) -> None:
        super().__init__("Generated document evidence unavailable")
        self.status = status


def parser_identity() -> str:
    from jarvis.knowledge import pdf_process
    code = Path(loader.__file__).read_bytes() + Path(pdf_process.__file__).read_bytes() + Path(pdf_process.__file__).with_name("pdf_worker.py").read_bytes()
    return "knowledge-pdf-job-v1:" + hashlib.sha256(code).hexdigest()


def generated_pdf_packet(raw: bytes, *, now: datetime | None = None) -> dict[str, Any]:
    """Retain immutable inline originals in the private existing review packet."""
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        raise ValueError("Timezone-aware observation required")
    artifact = process_pdf(raw)
    sha = artifact["source_sha256"]
    return {"source_kind": "generated_pdf", "parser_version": parser_identity(),
        "source_contract": dict(CONTRACT), "apply_allowed": False,
        "pdf_sources": [{"sha256": sha, "original_base64": base64.b64encode(raw).decode("ascii"),
                         "artifact": artifact, "received_at": clock.isoformat()}], "groups": [],
        "items": [{"source_kind": "generated_pdf", "source_sha256": sha, "pdf_index": c["chunk_index"],
                   "operation": {"kind": "document_chunk"}, "original_record": c,
                   "classification": "document_pending_review", "candidates": []} for c in artifact["chunks"]]}


def _original(source: dict[str, Any], now: datetime, max_age_seconds: float) -> dict[str, Any]:
    if "original_base64" not in source or not source["original_base64"]:
        raise PdfEvidenceUnavailable("unavailable")
    if set(source) != {"sha256", "original_base64", "artifact", "received_at"}:
        raise PdfEvidenceUnavailable("evidence_mismatch")
    if not isinstance(source["original_base64"], str) or len(source["original_base64"]) > 12 * 1024 * 1024:
        raise PdfEvidenceUnavailable("corrupt")
    raw = base64.b64decode(source["original_base64"], validate=True)
    if source["sha256"] != hashlib.sha256(raw).hexdigest():
        raise PdfEvidenceUnavailable("source_changed")
    artifact = source["artifact"]
    if artifact["parser_version"] != "pypdf:" + version("pypdf") + ";pages-v1":
        raise PdfEvidenceUnavailable("parser_changed")
    try:
        KnowledgeStore.verify_pdf_bytes(raw, artifact)
    except PdfWorkerUnavailable:
        raise PdfEvidenceUnavailable("unavailable") from None
    except KnowledgeParseError:
        raise PdfEvidenceUnavailable("corrupt") from None
    except KnowledgeUnavailable:
        raise PdfEvidenceUnavailable("evidence_mismatch") from None
    if source["received_at"] is None:
        raise PdfEvidenceUnavailable("unknown")
    received = datetime.fromisoformat(source["received_at"])
    if received.tzinfo is None:
        raise PdfEvidenceUnavailable("unknown")
    if not 0 <= (now - received).total_seconds() <= max_age_seconds:
        raise PdfEvidenceUnavailable("stale")
    return dict(artifact)


def assess_sources(packet: dict[str, Any], now: datetime, *, items: list[dict[str, Any]] | None = None,
                   expected_revision: int | None = None, max_age_seconds: float = 86400) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Assess original lineage and current review state, not financial source acceptance."""
    if now.tzinfo is None or not 0 < max_age_seconds <= 86400:
        raise ValueError("Aware clock and bounded freshness required")
    states: list[dict[str, Any]] = []
    originals: dict[str, dict[str, Any]] = {}
    status = "available"
    revision = max((i["revision"] for i in items or []), default=0)
    try:
        if packet.get("source_kind") != "generated_pdf":
            raise PdfEvidenceUnavailable("unknown")
        if packet.get("source_contract") != CONTRACT:
            raise PdfEvidenceUnavailable("evidence_mismatch")
        if packet.get("parser_version") != parser_identity():
            raise PdfEvidenceUnavailable("parser_changed")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != revision):
            raise PdfEvidenceUnavailable("evidence_mismatch")
        if not isinstance(packet.get("pdf_sources"), list) or not packet["pdf_sources"]:
            raise PdfEvidenceUnavailable("unavailable")
        for source in packet["pdf_sources"]:
            sha = source.get("sha256")
            source_status = "available"
            try:
                if sha in originals:
                    raise PdfEvidenceUnavailable("evidence_mismatch")
                if any(i["source_sha256"] == sha and i["status"] == "reject" for i in items or []):
                    raise PdfEvidenceUnavailable("revoked")
                artifact = _original(source, now, max_age_seconds)
                for item in items or []:
                    if item["source_sha256"] == sha and (item["source_kind"] != "generated_pdf"
                            or item["operation"] != {"kind": "document_chunk"}
                            or item["original_record"] not in artifact["chunks"]
                            or type(item["pdf_index"]) is not int or item["pdf_index"] != item["original_record"]["chunk_index"]):
                        raise PdfEvidenceUnavailable("evidence_mismatch")
                originals[sha] = artifact
            except PdfEvidenceUnavailable as error:
                source_status = error.status
            except (ValueError, KeyError, TypeError, AttributeError):
                source_status = "corrupt"
            states.append({"source_sha256": sha if isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha) else None,
                           "status": source_status, "last_attempt": now.isoformat(),
                           "last_success": now.isoformat() if source_status == "available" else None,
                           "observed_at": source.get("received_at") if source_status in {"available", "stale"} else None})
        if items is not None and (not items or any(i["source_sha256"] not in {s.get("sha256") for s in packet["pdf_sources"]} for i in items)):
            raise PdfEvidenceUnavailable("evidence_mismatch")
        status = next((s["status"] for s in states if s["status"] != "available"), "available")
    except PdfEvidenceUnavailable as error:
        status = error.status
    except (ValueError, KeyError, TypeError, AttributeError):
        status = "corrupt"
    except OSError:
        status = "unavailable"
    return {"status": status, "availability_status": "available" if status == "available" else "stale" if status == "stale" else "unavailable",
            "sources": states, "review_revision": revision, "source_error": None if status == "available" else status,
            "checked_at": now.isoformat(), "financial_source_approved": False, "database_modified": False}, originals


def verified_sources(packet: dict[str, Any], now: datetime) -> dict[str, dict[str, Any]]:
    state, sources = assess_sources(packet, now)
    if state["status"] != "available":
        raise PdfEvidenceUnavailable(state["status"])
    return sources


def generated_source_state(store: ReviewStore, *, now: datetime | None = None,
                           expected_revision: int | None = None, max_age_seconds: float = 86400) -> dict[str, Any]:
    """Inspect a verified review snapshot; an old restore is coherent, not globally latest."""
    import json
    import sqlite3

    from jarvis.finance.review_backup import review_snapshot
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        raise ValueError("Aware clock required")
    try:
        with review_snapshot(store) as frozen:
            meta = frozen.metadata()
            packet = json.loads(meta["packet"])
            if packet.get("source_kind") == "generated_pdf" and meta["parser_version"] != parser_identity():
                packet = {**packet, "parser_version": meta["parser_version"]}
            state, _ = assess_sources(packet, clock,
                items=frozen.items(), expected_revision=expected_revision, max_age_seconds=max_age_seconds)
            return state
    except (ValueError, TypeError, KeyError, sqlite3.Error):
        status = "corrupt"
    except OSError:
        status = "unavailable"
    return {"status": status, "availability_status": "unavailable", "source_error": status,
            "sources": [], "review_revision": None, "checked_at": clock.isoformat(),
            "financial_source_approved": False, "database_modified": False}
