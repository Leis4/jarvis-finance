"""Disposable synthetic workflow only; never a production daemon."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.review_store import ReviewStore, encoded
from jarvis.finance.scheduler.agent import AgentRuntime
from jarvis.finance.scheduler.service import SchedulerService


def demo_runtime(folder: Path) -> AgentRuntime:
    folder = folder.resolve()
    marker = folder / "synthetic-workflow.json"
    expected = {"schema": "synthetic-workflow-v1", "bank_access": False, "paid_calls": False}
    if folder.exists():
        if folder.is_symlink() or not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text(encoding="utf-8")) != expected:
            raise ValueError("Only an existing owned synthetic workspace can resume")
    else:
        folder.mkdir(parents=True)
        marker.write_text(encoded(expected), encoding="utf-8")
        review = ReviewStore(folder / "review")
        review.initialize({"apply_allowed": False, "items": [{"source_sha256": hashlib.sha256(b"synthetic-only").hexdigest(),
            "pdf_index": index, "classification": "likely", "candidates": [], "local_account": "synthetic-" + str(index % 3),
            "operation": {"tx_date": "2026-08-01", "amount": "-10.00", "currency": "RUB", "account": "synthetic",
                          "merchant": "magnit" if index % 3 == 0 else "unknown synthetic merchant",
                          "description": "magnit" if index % 3 == 0 else "unknown synthetic merchant"}} for index in range(500)]})
        db = FinanceDB(folder / "agent.sqlite")
        db.init()
        db.set_setting("agent_runtime_scope", "private_staging")
    return AgentRuntime(FinanceDB(folder / "agent.sqlite"), ReviewStore(folder / "review"))


async def rehearsal(folder: Path, *, seconds: float = 0, batches: int = 1) -> dict[str, Any]:
    if not 0 <= seconds <= 30 or not 1 <= batches <= 50:
        raise ValueError("Bounded rehearsal required")
    core = demo_runtime(folder)
    scheduler = SchedulerService(str(core.journal.path))
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    start = time.monotonic()
    result: dict[str, Any] = {}
    for _ in range(batches):
        result = await scheduler.agent_workflow(core, "synthetic-source-refresh", deadline=deadline)
        if result.get("queue_exhausted") or seconds == 0 or time.monotonic() - start >= seconds:
            break
    page = result.get("data", {}).get("result", {}).get("findings", {}).get("classify_pending", {})
    return {"mode": "synthetic_only", "state": result["state"], "processed_total": page.get("processed_total", result.get("processed_total")),
            "remaining": page.get("not_processed", result.get("remaining")), "task_id": result.get("task_id"),
            "production_database_modified": False, "paid_calls": 0, "scheduler_started": scheduler.scheduler.running,
            "human_labels_created": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic-only source-event rehearsal")
    parser.add_argument("--demo-workspace", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run-once", action="store_true")
    mode.add_argument("--run-for", type=float)
    parser.add_argument("--max-batches", type=int, default=50)
    args = parser.parse_args()
    result = asyncio.run(rehearsal(args.demo_workspace, seconds=args.run_for or 0, batches=1 if args.run_once else args.max_batches))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
