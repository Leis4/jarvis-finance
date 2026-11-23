"""Inspect saved source evidence and recovery on private copies, never probe live."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.finance.readiness import saved_source_entry
from jarvis.finance.review_backup import restore_review_backup, review_snapshot
from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_sources import source_registry
from jarvis.finance.review_staging import _guard
from jarvis.finance.review_store import ReviewStore, encoded


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stamp(value: Any) -> datetime | None:
    if value is None:
        return None
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError("Evidence timestamp needs timezone")
    return stamp


def _entry(status: str = "unavailable", **evidence: Any) -> dict[str, Any]:
    return {"status": status, "coverage": None, "last_success": None, "last_attempt": None,
            "source_error": None, "database_modified": False, **evidence}


def source_availability(store: ReviewStore, *, now: datetime | None = None,
                        max_age: timedelta = timedelta(hours=24)) -> dict[str, Any]:
    """Distinguish readable evidence, freshness and financial completeness."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or max_age.total_seconds() <= 0:
        raise ValueError("Timezone-aware now and positive freshness window required")
    checked = current.isoformat()
    errors = (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error)
    sources: dict[str, Any] = {}
    inputs_path = store.workspace / "availability-inputs.json"
    inputs: dict[str, Any] = {}
    generated: dict[str, Any] | None = None
    if inputs_path.exists():
        try:
            inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
            if not isinstance(inputs, dict):
                raise TypeError("Invalid availability inputs")
        except errors as exc:
            inputs = {}
            sources["configuration"] = _entry(source_error=type(exc).__name__)
    try:
        with review_snapshot(store) as frozen:
            meta = frozen.metadata()
            registry = source_registry(frozen)
            identity = encoded({"meta": meta, "events": frozen.audit(), "labels": HumanLabels(frozen).audit()})
            if json.loads(meta["packet"]).get("source_kind") == "generated_pdf":
                from jarvis.finance.review_pdf_evidence import generated_source_state
                generated = generated_source_state(frozen, now=current, max_age_seconds=min(max_age.total_seconds(), 86400))
    except errors as exc:
        return {"checked_at": checked, "database_modified": False, "status": "unavailable",
                "sources": {name: _entry("corrupt" if name == "generated_pdf" else "unavailable",
                            source_error="Review evidence unavailable: " + type(exc).__name__,
                            **({"financial_source_approved": False} if name == "generated_pdf" else {}))
                            for name in ["sber", "moex", "telegram", "local_pdf", "generated_pdf", "local_csv", "sqlite", "backup", "recovery", "rehearsal"]}}
    sources["local_pdf"] = _entry("insufficient_data", source_error="No registered originals")
    if generated is not None:
        sources["generated_pdf"] = {**_entry(generated["status"], last_attempt=checked), **generated,
            "coverage": {"document_observation_only": True, "ledger_accepted": False}}
    if registry:
        results = []
        for preview in registry["previews"]:
            try:
                name = preview["source"]["path_name"]
                if Path(name).name != name:
                    raise ValueError("Unsafe original filename")
                if _hash(Path(registry["folder"]) / name) != preview["source"]["sha256"]:
                    raise ValueError("Original hash mismatch")
                results.append({"status": "available", "sha256": preview["source"]["sha256"],
                                "period": preview["metadata"]["period"]})
            except errors as exc:
                results.append({"status": "unavailable", "source_error": type(exc).__name__})
        ok = bool(results) and all(r["status"] == "available" for r in results)
        sources["local_pdf"] = _entry("available" if ok else "unavailable", last_attempt=checked,
                                      last_success=checked if ok else None,
                                      source_error=None if ok else "Originals incomplete or changed",
                                      coverage={"files": results, "ledger_accepted": False})
    csv_results = []
    try:
        for original in inputs.get("local_csv", []):
            if _hash(Path(original["path"])) != original["sha256"]:
                raise ValueError("CSV hash mismatch")
            csv_results.append({"sha256": original["sha256"], "status": "available"})
        sources["local_csv"] = _entry("available" if csv_results else "insufficient_data",
                                      last_attempt=checked if csv_results else None,
                                      last_success=checked if csv_results else None,
                                      coverage={"files": csv_results, "ledger_accepted": False} if csv_results else None,
                                      source_error=None if csv_results else "No hash-registered CSV originals")
    except errors as exc:
        sources["local_csv"] = _entry(source_error=type(exc).__name__, last_attempt=checked)
    sources["sqlite"] = _entry(source_error="No managed rehearsal")
    sources["rehearsal"] = _entry("insufficient_data", source_error="No managed rehearsal")
    states: dict[str, Any] = {}
    try:
        latest = json.loads((store.workspace / "last-rehearsal.json").read_text(encoding="utf-8"))
        candidate = _guard(store, latest["batch_id"])
        with closing(sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity failure")
            states = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT key,value FROM settings WHERE key LIKE 'source_health:%'")}
        sources["sqlite"] = _entry("insufficient_data", last_attempt=checked, last_success=checked,
                                   coverage={"integrity": "ok", "ledger_accepted": False},
                                   source_error="Ledger coverage not accepted")
        manifest = json.loads((candidate.parent / "manifest.json").read_text(encoding="utf-8"))
        if not latest["restore_verified"] or any(_hash(candidate.parent / name) != manifest["backup_sha256"]
                                                for name in ["before.sqlite", "restore-verification.sqlite"]):
            raise ValueError("Rehearsal restore hash mismatch")
        observed = datetime.fromtimestamp((candidate.parent / "report.json").stat().st_mtime, UTC)
        sources["rehearsal"] = _entry("stale" if observed > current or current - observed > max_age else "available",
                                      last_attempt=checked, last_success=checked, freshness_basis="report_file_mtime",
                                      observed_artifact_time=observed.isoformat(),
                                      coverage={"restore_verified": True, "financial_accepted": False,
                                                "approved_new": latest["first"]["approved_new"]})
    except errors as exc:
        if sources["sqlite"]["last_success"] is None:
            sources["sqlite"] = _entry(source_error=type(exc).__name__, last_attempt=checked)
        sources["rehearsal"] = _entry(source_error=type(exc).__name__, last_attempt=checked)
    for name, key in [("sber", "sber_mcp"), ("telegram", "telegram")]:
        sources[name] = _entry("blocked", source_error="Live access not probed; separate acceptance required",
                               mode="saved_evidence_only", live_probe_performed=False)
        if "source_health:" + key in states:
            sources[name] = saved_source_entry(states["source_health:" + key], current, max_age)
    sources["moex"] = _entry("blocked", source_error="No configured corporate-action evidence", live_probe_performed=False)
    if inputs.get("moex_coverage"):
        try:
            report = json.loads(Path(inputs["moex_coverage"]).read_text(encoding="utf-8"))
            stamp = _stamp(report["created_at"])
            sources["moex"] = _entry("blocked", last_attempt=stamp.isoformat() if stamp else None,
                                     coverage=report["summary"], source_error="Corporate-action completeness/semantics not accepted",
                                     live_probe_performed=False, price_only_preserved=True)
        except errors as exc:
            sources["moex"] = _entry(source_error=type(exc).__name__)
    sources["backup"] = sources["recovery"] = _entry(source_error="No review backup")
    try:
        archives = list((store.workspace / "backups").glob("*.zip"))
        if archives:
            archive = max(archives, key=lambda p: p.stat().st_mtime)
            with tempfile.TemporaryDirectory(prefix="jarvis-readiness-recovery-") as temporary:
                restored = Path(temporary) / "restored"
                restore_review_backup(archive, restored)
                recovered = ReviewStore(restored)
                matches = encoded({"meta": recovered.metadata(), "events": recovered.audit(),
                                   "labels": HumanLabels(recovered).audit()}) == identity
            with zipfile.ZipFile(archive) as contents:
                created = _stamp(json.loads(contents.read("manifest.json"))["created_at"])
            stale = not matches or created is None or created > current or current - created > max_age
            sources["backup"] = _entry("stale" if stale else "available", last_success=created.isoformat() if created else None,
                                       last_attempt=checked, coverage={"matches_current_review": matches},
                                       source_error="Backup old or review changed" if stale else None)
            sources["recovery"] = _entry("stale" if not matches else "available", last_success=checked,
                                         last_attempt=checked, coverage={"restore_verified": True, "matches_current_review": matches},
                                         source_error=None if matches else "Recovery omits later review changes")
    except (*errors, zipfile.BadZipFile) as exc:
        sources["backup"] = sources["recovery"] = _entry(source_error=type(exc).__name__, last_attempt=checked)
    from jarvis.finance.review_provenance import provenance_matrix
    provenance = provenance_matrix(store)
    sources["rehearsal_provenance"] = _entry(provenance["status"], source_error=provenance["reason"],
                                             coverage={"binding": provenance["binding"], "financial_accepted": False})
    return {"checked_at": checked, "freshness_window_seconds": max_age.total_seconds(),
            "status": "blocked", "database_modified": False, "sources": sources,
            "note": "File availability and saved successes do not accept production or prove live connectivity."}
