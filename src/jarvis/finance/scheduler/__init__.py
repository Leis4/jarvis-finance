from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from jarvis.finance.backup import create_backup
from jarvis.finance.db import FinanceDB
from jarvis.finance.optimizer import build_optimization_plan, plan_limit_update_action
from jarvis.finance.rules import DEFAULT_RULES
from jarvis.finance.savings import plan_month_end_reserve_sweep

SUPPORTED_TASK_TYPES = {"optimize_limits", "reserve_sweep", "backup"}


@dataclass(frozen=True)
class ScheduledTask:
    id: int
    name: str
    task_type: str
    interval_days: int
    next_run_date: date
    params: dict[str, Any]
    enabled: bool


@dataclass(frozen=True)
class TaskRunResult:
    task_id: int
    task_type: str
    status: str
    message: str


def row_to_task(row: dict[str, Any]) -> ScheduledTask:
    return ScheduledTask(
        id=int(row["id"]),
        name=str(row["name"]),
        task_type=str(row["task_type"]),
        interval_days=int(row["interval_days"]),
        next_run_date=date.fromisoformat(str(row["next_run_date"])),
        params=json.loads(str(row.get("params") or "{}")),
        enabled=bool(row["enabled"]),
    )


def create_scheduled_task(
    db: FinanceDB,
    name: str,
    task_type: str,
    interval_days: int,
    next_run_date: date,
    params: dict[str, Any] | None = None,
) -> int:
    if task_type not in SUPPORTED_TASK_TYPES:
        raise ValueError(f"Unsupported task_type: {task_type}")
    if interval_days < 1:
        raise ValueError("interval_days must be >= 1")
    return db.create_scheduled_task(name, task_type, interval_days, next_run_date, params or {})


def list_tasks(db: FinanceDB, enabled_only: bool = False) -> list[ScheduledTask]:
    return [row_to_task(row) for row in db.list_scheduled_tasks(enabled_only=enabled_only)]


def run_task(db: FinanceDB, task: ScheduledTask, output_dir: str | Path = "./backups") -> TaskRunResult:
    try:
        if task.task_type == "optimize_limits":
            rules = db.get_setting("rules", DEFAULT_RULES)
            plan = build_optimization_plan(db, rules, model_dir=task.params.get("model_dir"))
            if not plan.rows:
                message = "Оптимизация пропущена: нет данных."
            else:
                action_id = plan_limit_update_action(db, plan)
                message = f"Создано действие budget_limit_update #{action_id}; экономия {plan.projected_monthly_savings} RUB/мес."
        elif task.task_type == "reserve_sweep":
            rules = db.get_setting("rules", DEFAULT_RULES)
            _action_id, message = plan_month_end_reserve_sweep(db, rules)
        elif task.task_type == "backup":
            result = create_backup(db.path, output_dir=task.params.get("output_dir") or output_dir)
            message = f"Создан backup: {result.path}"
        else:
            return TaskRunResult(task.id, task.task_type, "failed", f"Unsupported task_type: {task.task_type}")
        return TaskRunResult(task.id, task.task_type, "ok", message)
    except Exception as exc:  # noqa: BLE001 - scheduler must record failures instead of crashing batch runs
        return TaskRunResult(task.id, task.task_type, "failed", f"{type(exc).__name__}: {exc}")


from datetime import UTC, datetime


def run_due_tasks(db: FinanceDB, today: date | None = None, output_dir: str | Path = "./backups") -> list[TaskRunResult]:
    today = today or datetime.now(UTC).date()
    results: list[TaskRunResult] = []
    for task in list_tasks(db, enabled_only=True):
        if task.next_run_date > today:
            continue
        result = run_task(db, task, output_dir=output_dir)
        db.record_scheduled_task_run(task.id, today, result.status, result.message)
        db.update_scheduled_task_next_run(task.id, task.next_run_date + timedelta(days=task.interval_days))
        results.append(result)
    return results


def render_tasks(tasks: list[ScheduledTask]) -> str:
    if not tasks:
        return "Scheduled tasks not found."
    lines = ["Scheduled tasks:", ""]
    for t in tasks:
        state = "enabled" if t.enabled else "disabled"
        lines.append(f"#{t.id} {t.name}: {t.task_type}, every {t.interval_days}d, next={t.next_run_date}, {state}, params={t.params}")
    return "\n".join(lines)


def render_run_results(results: list[TaskRunResult]) -> str:
    if not results:
        return "Due scheduled tasks not found."
    lines = ["Scheduled task results:", ""]
    for result in results:
        lines.append(f"- task #{result.task_id} {result.task_type}: {result.status}. {result.message}")
    return "\n".join(lines)
