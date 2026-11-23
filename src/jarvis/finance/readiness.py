"""Saved financial evidence readiness; never probe sources or create databases."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

PRIME_GATE_DEPENDENCIES: dict[str, list[str]] = {
    "supply_chain": [], "operator_isolation": ["supply_chain"],
    "sdk_privacy": ["supply_chain", "operator_isolation"],
    "readonly_session": ["operator_isolation", "sdk_privacy"],
    "products_balances": ["readonly_session"], "bounded_operations": ["readonly_session"],
    "monetary_semantics": ["products_balances", "bounded_operations"],
    "history_coverage": ["monetary_semantics"],
    "ledger_reconciliation": ["history_coverage", "human_review"], "human_review": [],
    "dashboard": ["ledger_reconciliation"], "scheduled_reads": ["readonly_session", "recovery_deployment"],
    "source_explanations": ["ledger_reconciliation"], "classification": ["human_review"],
    "knowledge": [], "investment_research": ["supply_chain"], "recovery_deployment": [],
}
PRIME_STATUSES = frozenset({"IMPLEMENTED", "SYNTHETIC_VERIFIED", "OS_VERIFIED",
                          "HUMAN_ACCEPTED", "REAL_SOURCE_VERIFIED", "OPERATIONALLY_VERIFIED", "BLOCKED"})


def prime_manifest_summary(manifest: Any, evidence: dict[str, bytes], *, now: datetime) -> dict[str, Any]:
    """Validate offline evidence bindings; manifest claims never authorize banking."""
    def invalid() -> None:
        raise ValueError("Invalid Prime acceptance manifest")

    if now.tzinfo is None or not isinstance(manifest, dict):
        invalid()
    if manifest.get("format") != "jarvis-prime-acceptance-v1" or manifest.get("scope") != "offline_tracking_only":
        invalid()
    if any(manifest.get(key) is not False for key in ("live_allowed", "apply_allowed", "production_ready")):
        invalid()
    gates = manifest.get("gates")
    if not isinstance(gates, list) or len(gates) != len(PRIME_GATE_DEPENDENCIES):
        invalid()
    ids = [gate.get("id") if isinstance(gate, dict) else None for gate in gates]
    if any(type(ident) is not str for ident in ids) or set(ids) != set(PRIME_GATE_DEPENDENCIES):
        invalid()
    rows = []
    for gate in gates:
        ident = gate["id"]
        if (gate.get("depends_on") != PRIME_GATE_DEPENDENCIES[ident]
                or type(gate.get("status")) is not str or gate["status"] not in PRIME_STATUSES):
            invalid()
        if any(type(gate.get(key)) is not str or not 1 <= len(gate[key]) <= 2000 for key in ("owner", "criteria", "rollback")):
            invalid()
        refs = gate.get("evidence")
        if not isinstance(refs, list) or len(refs) > 32:
            invalid()
        bound = 0
        seen: set[str] = set()
        for ref in refs:
            if not isinstance(ref, dict) or type(ref.get("id")) is not str or ref["id"] in seen:
                invalid()
            seen.add(ref["id"])
            data = evidence.get(ref["id"])
            if data is None or type(data) is not bytes or hashlib.sha256(data).hexdigest() != ref.get("sha256"):
                invalid()
            bound += 1
        stamp = gate.get("last_verified_at")
        if stamp is not None:
            try:
                verified = datetime.fromisoformat(stamp)
                if verified.tzinfo is None or verified > now or not refs:
                    invalid()
            except (ValueError, TypeError):
                invalid()
        if gate["status"] != "BLOCKED" and (not refs or stamp is None):
            invalid()
        rows.append({"id": ident, "claimed_status": gate["status"], "bound_evidence_count": bound,
                     "human_or_os_authenticity_verified": False})
    return {"format": "jarvis-prime-summary-v1", "status": "BLOCKED", "gates": rows,
            "live_allowed": False, "apply_allowed": False, "production_ready": False,
            "database_modified": False, "limitation": "Hash binding records claims, not human/OS/source acceptance"}


@contextmanager
def readonly_snapshot(path: Path) -> Iterator[tuple[Path, str]]:
    """Yield a disposable snapshot without recovery or creation of the source."""
    if not path.is_file() or any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("Stable SQLite snapshot required")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="jarvis-readonly-") as folder:
        copy = Path(folder) / "snapshot.sqlite"
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as source, closing(sqlite3.connect(copy)) as target:
            source.backup(target)
        if hashlib.sha256(path.read_bytes()).hexdigest() != before:
            raise ValueError("Source changed during snapshot")
        yield copy, before
        if hashlib.sha256(path.read_bytes()).hexdigest() != before:
            raise ValueError("Source changed during inspection")


def saved_source_entry(
    state: Any, now: datetime, max_age: timedelta = timedelta(hours=24),
) -> dict[str, Any]:
    """Separate source observation, freshness and unknown financial completeness."""
    if now.tzinfo is None or max_age.total_seconds() <= 0:
        raise ValueError("Aware clock and positive freshness window required")
    entry: dict[str, Any] = {
        "status": "unavailable", "coverage": None, "last_success": None, "last_attempt": None,
        "age_seconds": None, "source_error": "No saved source evidence", "database_modified": False,
        "mode": "saved_evidence_only", "live_probe_performed": False, "live_actions_blocked": True,
    }
    if state is None or (isinstance(state, dict) and not state):
        return entry
    try:
        if not isinstance(state, dict):
            raise TypeError
        success = datetime.fromisoformat(state["last_success_at"]) if state.get("last_success_at") else None
        attempt = datetime.fromisoformat(state["checked_at"]) if state.get("checked_at") else None
        if any(stamp.tzinfo is None for stamp in (success, attempt) if stamp):
            raise ValueError
        coverage_end = state.get("coverage_end")
        if coverage_end is not None:
            coverage_end = date.fromisoformat(coverage_end).isoformat()
        age = (now - success).total_seconds() if success else None
        status = state.get("status")
        if status == "blocked":
            status, reason = "blocked", "Source actions blocked"
        elif status != "ok":
            status, reason = "unavailable", "Recorded source failure (redacted)"
        elif success is None or attempt is None:
            status, reason = "insufficient_data", "Source observation time missing"
        elif success > now or attempt > now or now - success > max_age:
            status, reason = "stale", "Source evidence expired or clock inconsistent"
        elif success > attempt:
            status, reason = "unavailable", "Source timestamps inconsistent"
        else:
            status, reason = "available", None
        entry.update(status=status, source_error=reason, age_seconds=age,
                     last_success=success.isoformat() if success else None,
                     last_attempt=attempt.isoformat() if attempt else None,
                     coverage={"end": coverage_end, "complete": None})
    except (TypeError, ValueError, AttributeError):
        entry["source_error"] = "Invalid saved source evidence"
    return entry


def render_readiness(report: dict[str, Any]) -> str:
    """Render source states without treating local liveness as financial acceptance."""
    lines = [f"Financial readiness: {report['status']}",
             f"Checked: {report['checked_at']}", "Scope: saved evidence only; no live probes",
             "Coverage complete: false; production acceptance BLOCKED"]
    for name, source in sorted(report["sources"].items()):
        lines.append(f"{name}: {source['status']}; last success: {source.get('last_success') or 'unknown'}")
    lines.append("database_modified=false")
    return "\n".join(lines)


def local_readiness(db_path: Path, now: datetime | None = None) -> dict[str, Any]:
    """Inspect saved SQLite/source evidence without opening network connections."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("readiness time must be timezone-aware")
    sources: dict[str, dict[str, Any]] = {
        name: {"status": "blocked", "coverage": None, "last_success": None,
               "last_attempt": None, "source_error": "Live source not probed"}
        for name in ("sber", "moex", "telegram")
    }
    sources["pdf"] = {"status": "insufficient_data", "coverage": None,
                       "last_success": None, "last_attempt": None,
                       "source_error": "Review workspace not attached to API"}
    sources["csv"] = dict(sources["pdf"])
    database = {"status": "unavailable", "coverage": None,
                "last_success": None, "last_attempt": None, "source_error": "SQLite missing"}
    if db_path.is_file():
        try:
            if any(db_path.with_name(db_path.name + suffix).exists() for suffix in ("-wal", "-journal")):
                raise RuntimeError("SQLite WAL requires a consistent snapshot")
            with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("SQLite integrity check failed")
                rows = conn.execute("SELECT key,value FROM settings WHERE key LIKE 'source_health:%'").fetchall()
            database = {"status": "available", "coverage": None, "last_success": None,
                        "last_attempt": current.isoformat(), "source_error": None}
            for key, value in rows:
                name = {"sber_mcp": "sber", "moex_mcp": "moex", "telegram": "telegram"}.get(
                    key.removeprefix("source_health:"))
                if name is None:
                    continue
                try:
                    state = json.loads(value)
                    sources[name] = saved_source_entry(state, current)
                except (TypeError, ValueError, AttributeError):
                    sources[name] = {"status": "unavailable", "coverage": None,
                                     "last_success": None, "last_attempt": None,
                                     "source_error": "Invalid saved source evidence"}
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            database["source_error"] = type(exc).__name__
    sources["sqlite"] = database
    for entry in sources.values():
        entry.setdefault("database_modified", False)
        entry.setdefault("mode", "saved_evidence_only")
        entry.setdefault("live_probe_performed", False)
        entry.setdefault("live_actions_blocked", True)
    return {"status": "unavailable" if database["status"] == "unavailable" else "insufficient_data",
            "scope": "local_readiness", "checked_at": current.isoformat(),
            "coverage_complete": False, "database_modified": False, "sources": sources}
