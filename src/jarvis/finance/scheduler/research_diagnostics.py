"""Read-only, offline inspection of a recorded research task and RUB binding."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from jarvis.core.llm.budget_store import BudgetStore
from jarvis.core.llm.planner_diagnostics import sanitized


def inspect_task(agent_journal: Path, task_id: int, budget_journal: Path | None = None,
                 *, simulation: bool = False) -> dict[str, Any]:
    """Read existing rows only; do not reconstruct or run the agent."""
    if task_id < 1 or not agent_journal.is_file() or agent_journal.is_symlink():
        raise ValueError("Existing task journal required")
    with closing(sqlite3.connect(agent_journal.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        row = conn.execute("SELECT t.params,r.status,r.message FROM scheduled_tasks t JOIN scheduled_task_runs r ON r.task_id=t.id WHERE t.id=? AND t.task_type='agent_core' ORDER BY r.id DESC LIMIT 1", (task_id,)).fetchone()
        version, identity, grant_hash = "public-research-plan-v1", None, None
        if row is not None:
            saved = json.loads(row[0])
            grant_id = saved.get("research_policy_id") if isinstance(saved, dict) else None
            if isinstance(grant_id, str):
                grant_hash = hashlib.sha256(grant_id.encode()).hexdigest()
                grant = conn.execute("SELECT payload FROM audit_log WHERE event_type='research_policy_granted' AND json_extract(payload,'$.policy_id')=? ORDER BY id DESC LIMIT 1", (grant_id,)).fetchone()
                if grant is not None and json.loads(grant[0]).get("identity_version", 1) == 2:
                    version = "public-research-plan-v2"
                    invocation = conn.execute("SELECT payload FROM audit_log WHERE event_type='research_invocation_bound' AND json_extract(payload,'$.key')=? ORDER BY id DESC LIMIT 1", (f"research_invocation:{grant_id}:{task_id}",)).fetchone()
                    if invocation is not None:
                        identity = json.loads(invocation[0])["binding"]["fingerprint"]
                        if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
                            raise ValueError("Invalid immutable invocation")
    if row is None or any(len(str(item)) > 64_000 for item in row):
        raise ValueError("Bounded research task required")
    params, data = json.loads(row[0]), json.loads(row[2])
    if not isinstance(params, dict) or params.get("intent") != "public_research" or not isinstance(data, dict):
        raise ValueError("Research task required")
    revision = params.get("input_revision")
    if not isinstance(revision, str) or len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("Task revision invalid")
    if version == "public-research-plan-v1":
        identity = hashlib.sha256(("public-research-plan-v1:" + str(task_id) + revision + "qwen3.7-flash").encode()).hexdigest()
    state = row[1]
    if state not in {"QUEUED", "RUNNING", "UNKNOWN", "COMPLETED", "FAILED", "CANCELLED", "WAITING_FOR_DATA", "WAITING_FOR_APPROVAL"}:
        raise ValueError("Task state invalid")
    diagnostic = sanitized(data.get("diagnostics"))
    observation = data.get("observation")
    if not diagnostic and isinstance(observation, dict):
        diagnostic = sanitized(observation.get("diagnostics"))
    if not diagnostic:
        diagnostic = sanitized(data.get("model_completion"))
    result: dict[str, Any] = {"task_id": task_id, "state": state, "diagnostics": diagnostic,
        "identity_version": version, "grant_identity_sha256": grant_hash,
        "request_identity": identity, "existing_replay": None,
        "external_transport_may_have_been_attempted": None, "debit_evidence": "not_inspected",
        "workspace_task_binding": hashlib.sha256((str(task_id)+revision+(grant_hash or "legacy")).encode()).hexdigest(),
        "historical_detail": "recorded" if diagnostic else "not_recorded",
        "request_evidence": diagnostic.get("request_evidence", "historical_unknown"),
        "billing": "not_inspected", "production_database_modified": False,
        "journal_modified": False, "http_requests": 0, "approval_issued": False}
    if data.get("reason") in {"tool data unavailable", "public sources changed before result", "source changed before result"}:
        result["failure_stage"] = "sources"
    if data.get("failed_tool") in {"read_public_sources", "explain_public_sources"}:
        result["failure_stage"] = "sources"
    if budget_journal is not None:
        if not budget_journal.is_file() or budget_journal.is_symlink():
            raise ValueError("Existing budget journal required")
        store = BudgetStore(budget_journal, currency="RUB", simulation=simulation)
        binding = store.request_status(identity) if identity is not None else None
        result["request_identity"] = identity
        result["existing_replay"] = binding is not None
        if binding is None:
            result["billing"] = "no_matching_binding"
            # Absence is not proof that an HTTP request was never sent.
        else:
            status = binding["status"]
            if status not in {"reserved", "attempted", "uncertain", "settled", "released"}:
                raise ValueError("Budget state invalid")
            money = sanitized({"cost_rub": binding["settled"]})
            reserve = sanitized({"cost_rub": binding["reserved"]})
            result.update(billing=status, reserved_rub=reserve.get("cost_rub"),
                          settled_rub=money.get("cost_rub") if status == "settled" else None)
            if status in {"attempted", "uncertain"}:
                result["debit_evidence"] = "unknown"
                result["external_transport_may_have_been_attempted"] = True
            elif status == "settled":
                result["debit_evidence"] = "local_usage_receipt"
                result["external_transport_may_have_been_attempted"] = True
            else:
                result["external_transport_may_have_been_attempted"] = False
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline read-only research outcome; never resend")
    parser.add_argument("--agent-journal", type=Path, required=True)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--budget-journal", type=Path)
    parser.add_argument("--simulation", action="store_true")
    args = parser.parse_args()
    try:
        result = inspect_task(args.agent_journal, args.task_id, args.budget_journal, simulation=args.simulation)
    except (ValueError, OSError, sqlite3.Error, RuntimeError):
        print(json.dumps({"status": "unavailable", "reason": "Existing diagnostic evidence unavailable; no changes or HTTP"}))
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False))
    if result["state"] != "COMPLETED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
