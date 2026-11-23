"""Read-only scheduled financial reports from the existing Decimal ledger."""
from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.audit_report import build_audit_report
from jarvis.finance.db import FinanceDB
from jarvis.finance.ledger import cashflow, finite_money
from jarvis.finance.readiness import local_readiness, readonly_snapshot


def _build(db_path: str, days: int, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Aware report clock required")
    end = current.astimezone(UTC).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    data: dict[str, Any] = {
        "date": end.isoformat(), "week_start": start.isoformat(), "week_end": end.isoformat(),
        "period": {"start": start.isoformat(), "end": end.isoformat()}, "checked_at": current.isoformat(),
        "status": "unavailable", "balance": None, "free_cash": None, "operations": None,
        "currencies": {}, "source_error": None, "database_modified": False,
        "mode": "saved_evidence_only", "coverage_complete": False,
        "formula": "income - expenses = net; net + excluded principal + unresolved = signed movement",
    }
    path = Path(db_path)
    readiness = local_readiness(path, current)
    data["readiness"] = readiness
    if readiness["sources"]["sqlite"]["status"] != "available":
        data["source_error"] = "Local SQLite unavailable or inconsistent"
        return data
    try:
        if path.with_name(path.name + "-journal").exists():
            raise ValueError("SQLite journal requires a consistent snapshot")
        with readonly_snapshot(path) as (copy, before):
            db = FinanceDB(copy)
            rows = db.all_transactions()
            expected = set(db.get_setting("expected_accounts", []))
            selected = [row for row in rows if start.isoformat() <= row["tx_date"] <= end.isoformat()]
            data["operations"] = selected
            currencies = sorted({str(row["currency"]) for row in rows})
            currencies = currencies or ["RUB"]  # unit only; unknown totals remain None
            for currency in currencies:
                report = build_audit_report(db, start, end, currency=currency, now=current)
                subset = [row for row in selected if row["currency"] == currency]
                unresolved = sum(row["category"] in {"pending_review", "uncategorized", "unknown", "other"}
                                 for row in subset)
                states = [report["source_health"][name]["availability_status"] for name in report["sources"]]
                complete = bool(expected) and report["status"] == "complete" and not unresolved and bool(states) and all(s == "available" for s in states)
                status = ("unavailable" if any(s in {"unavailable", "blocked"} for s in states) else
                          "stale" if "stale" in states else "available" if complete else "insufficient_data")
                flow = cashflow(subset, currency)
                totals = {key: finite_money(value) if value is not None else None
                          for key, value in report["totals"].items()}
                balance = sum((finite_money(a["reconciliation"]["closing"]) for a in report["accounts"]), Decimal(0)) if complete else None
                data["currencies"][currency] = {"status": status, "coverage_complete": complete,
                    "totals": totals, "balance": balance, "unresolved": unresolved,
                    "taxonomy_version": flow["taxonomy_version"], "evidence": report}
        data["source_sha256"] = before
        states = [entry["status"] for entry in data["currencies"].values()]
        data["status"] = ("unavailable" if "unavailable" in states else "stale" if "stale" in states else
                          "available" if all(s == "available" for s in states) else "insufficient_data")
        data["coverage_complete"] = all(entry["coverage_complete"] for entry in data["currencies"].values())
        data["balance"] = data["currencies"].get("RUB", {}).get("balance")
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        data.update(status="unavailable", source_error=type(exc).__name__, operations=None, currencies={})
    return data


def build_daily_report_data(db_path: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Report yesterday without network calls or production writes."""
    return _build(db_path, 1, now)


def build_weekly_report_data(db_path: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Report seven complete calendar days ending yesterday."""
    return _build(db_path, 7, now)


def read_only_job_manifest() -> dict[str, Any]:
    """Describe bounded report rehearsals without registering or executing jobs."""
    return {
        "policy_version": "saved-brief-v1", "mode": "plan_only", "complete_job_inventory": False,
        "jobs": [{"job": job, "schedule": schedule, "timezone": "Europe/Moscow",
                  "input_snapshots": ["consistent SQLite", "saved source health", "account coverage", "confirmed balances"],
                  "freshness_policy": "existing audit source policy; no live probes",
                  "read_write_class": "production_read_only_disposable_copy", "privacy_class": "private_finance",
                  "estimated_compute_cost": None, "cost_status": "unmeasured", "llm_calls": False,
                  "delivery_effect": "disabled_in_rehearsal", "retry_semantics": "manual replay of retained inputs",
                  "idempotency_contract": "policy/job/period/source SHA-256; artifact only, not delivery permission",
                  "unknown_delivery": "never automatically resend"}
                 for job, schedule in (("daily_report", "daily 08:30"), ("weekly_report", "Sunday 20:00"))],
        "database_modified": False, "scheduler_started": False, "jobs_registered": False,
        "runtime_status": "not_probed_actions_describe_this_plan_only",
    }


def scheduler_inventory(*, runner_path: Path | None = None) -> dict[str, Any]:
    """Inventory source registrations without importing runner or starting jobs."""
    base = Path(__file__).resolve().parent
    paths = {"service": base / "service.py", "standalone": runner_path or base.parents[3] / "scripts/run_scheduler.py"}
    jobs_raw = (base / "jobs.py").read_bytes()
    job_tree = ast.parse(jobs_raw)
    functions = {n.name: n for n in job_tree.body if isinstance(n, ast.FunctionDef)}
    retry = functions["_safe_run"]
    retry_default = next((ast.literal_eval(value) for arg, value in zip(retry.args.kwonlyargs, retry.args.kw_defaults, strict=True)
                          if arg.arg == "max_retries" and value is not None), None)
    requirements = {
        "backup_db": ["SQLite", "backup destination"], "healthcheck": ["saved source evidence", "SQLite"],
        "sync_sber": ["Sber live", "SQLite write acceptance"], "sber_keepalive": ["Sber live"],
        "market_scan_morning": ["MOEX live", "Telegram optional"],
        "retrain_setfit": ["confirmed human labels", "local model write acceptance"],
        "weekly_insights": ["SQLite", "LLM operator gate", "Telegram optional"],
        "system_health": ["SQLite", "saved source evidence", "logs", "Telegram optional"],
        "smart_notifications": ["SQLite", "Telegram"], "anomaly_check": ["SQLite", "Telegram optional"],
        "drift_check": ["SQLite", "model metadata"], "daily_metrics": ["SQLite"],
        **{name: ["SQLite", "complete account coverage", "confirmed balances", "fresh saved sources"]
           for name in ("daily_report", "weekly_report", "daily_forecast", "monthly_forecast", "forecast_update")},
    }
    manifests: list[dict[str, Any]] = []
    sources: dict[str, Any] = {}
    for registrar, path in paths.items():
        if not path.is_file():
            sources[registrar] = {"status": "unavailable", "source_sha256": None}
            continue
        try:
            raw = path.read_bytes()
            tree = ast.parse(raw)
        except (OSError, SyntaxError) as exc:
            sources[registrar] = {"status": "unavailable", "source_sha256": None, "error_type": type(exc).__name__}
            continue
        timezones = [ast.literal_eval(k.value) for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name) and node.func.id.endswith("Scheduler")
                     for k in node.keywords if k.arg == "timezone" and isinstance(k.value, ast.Constant)]
        sources[registrar] = {"status": "available", "source_sha256": hashlib.sha256(raw).hexdigest()}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "add_job":
                continue
            keywords = {k.arg: k.value for k in node.keywords}
            ident = ast.literal_eval(keywords["id"]) if isinstance(keywords.get("id"), ast.Constant) else None
            wrapper = node.args[0] if node.args else None
            target = wrapper.args[0].id if isinstance(wrapper, ast.Call) and wrapper.args and isinstance(wrapper.args[0], ast.Name) else None
            trigger = node.args[1] if len(node.args) > 1 else None
            schedule: dict[str, Any] = {"trigger": trigger.value if isinstance(trigger, ast.Constant) else
                        trigger.func.id if isinstance(trigger, ast.Call) and isinstance(trigger.func, ast.Name) else None}
            schedule["trigger"] = {"CronTrigger": "cron", "IntervalTrigger": "interval"}.get(schedule["trigger"], schedule["trigger"])
            params = trigger.keywords if isinstance(trigger, ast.Call) else node.keywords
            schedule.update({k.arg: ast.literal_eval(k.value) for k in params
                             if k.arg in {"hour", "minute", "day", "day_of_week", "hours", "minutes"}
                             and isinstance(k.value, ast.Constant)})
            effects = ["conditional logs"]
            if registrar == "service" or ident in {"daily_report", "weekly_report", "market_scan_morning", "anomaly_check", "system_health", "weekly_insights", "smart_notifications"}:
                effects += ["conditional Telegram send and delivery journal write"]
            if ident in {"sync_sber", "sber_keepalive", "market_scan_morning", "weekly_insights"}:
                effects += ["external source/provider access behind existing gates"]
            if ident in {"sync_sber", "retrain_setfit", "forecast_update", "drift_check", "backup_db"}:
                effects += ["conditional local database/model/backup write"]
            manifests.append({"registrar": registrar, "id": ident, "handler": target,
                "purpose": ast.get_docstring(functions[target]) if target in functions else None,
                "handler_found_in_jobs_source": target in functions,
                "schedule": schedule, "timezone": timezones[0] if len(set(timezones)) == 1 else None,
                "required_sources": requirements.get(str(ident)), "potential_effects": effects,
                "explicit_max_instances": ast.literal_eval(keywords["max_instances"]) if isinstance(keywords.get("max_instances"), ast.Constant) else None,
                "explicit_coalesce": ast.literal_eval(keywords["coalesce"]) if isinstance(keywords.get("coalesce"), ast.Constant) else None,
                "configured_timeout_seconds": None, "timeout_status": "not_configured_at_registration",
                "exception_attempt_limit": 1,
                "configured_retry_ceiling": retry_default if registrar == "standalone" else 1,
                "retry_semantics": "no automatic replay without explicit replay_safe proof",
                "delivery_state": "not_observed; persistent started/unknown must not resend",
                "idempotency": "job-specific; not proven for all effects", "readiness": "unverified",
                "launch_allowed": False, "launch_condition": "separate production acceptance and actual source readiness",
                "recovery": "retained inputs + job-specific journals; unknown effects need reconciliation",
                "compute_cost": None, "cost_status": "unknown"})
    disagreements = []
    for ident in sorted({str(j["id"]) for j in manifests}):
        variants = [j for j in manifests if j["id"] == ident]
        if len({json.dumps(j["schedule"], sort_keys=True) for j in variants}) > 1:
            disagreements.append({"id": ident, "variants": [{"registrar": j["registrar"], "schedule": j["schedule"]} for j in variants]})
    result = {"policy_version": "source-job-inventory-v1", "status": "blocked", "mode": "source_only",
        "complete_static_inventory": all(s["status"] == "available" for s in sources.values()) and
            all(j["id"] and j["handler_found_in_jobs_source"] and j["schedule"]["trigger"] in {"cron", "interval"} and j["required_sources"] for j in manifests),
        "runtime_inventory_verified": False, "registrars": sources, "jobs_source_sha256": hashlib.sha256(jobs_raw).hexdigest(),
        "jobs": manifests, "schedule_disagreements": disagreements,
        "database_modified": False, "jobs_registered": False, "scheduler_started": False,
        "source_errors": [name for name, source in sources.items() if source["status"] != "available"]}
    return {**result, "evidence_sha256": hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()}


def dry_run_daily_brief(db_path: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Return a private evidence-linked brief without delivery or journal writes."""
    data = build_daily_report_data(db_path, now=now)
    manifest = read_only_job_manifest()
    identity = {"policy_version": manifest["policy_version"], "job": "daily_report",
                "period": data["period"], "source_sha256": data.get("source_sha256")}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() if identity["source_sha256"] else None
    totals = None
    if data["status"] == "available" and data["coverage_complete"]:
        totals = {currency: {key: str(value) if value is not None else None for key, value in row["totals"].items()}
                  for currency, row in data["currencies"].items()}
    result = {**identity, "status": data["status"], "checked_at": data["checked_at"],
              "scope": "saved_report_rehearsal_not_production_acceptance", "coverage_complete": data["coverage_complete"],
              "source_error": data["source_error"], "totals_by_currency": totals,
              "idempotency_key": key, "manifest": manifest,
              "database_modified": False, "delivery_attempted": False, "journal_written": False}
    result["evidence_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def _render(data: dict[str, Any], title: str) -> str:
    lines = [title, f"{data.get('period', {}).get('start', 'unknown')} - {data.get('period', {}).get('end', 'unknown')}",
             f"Status: {data.get('status', 'insufficient_data')}; saved evidence only",
             "Observed subtotals are not total wealth or production acceptance."]
    for currency, entry in sorted(data.get("currencies", {}).items()):
        lines.append(f"{currency}: {entry['status']}; unresolved={entry['unresolved']}")
        for key in ("income", "expenses", "net", "movement", "excluded_movement", "unresolved_movement"):
            value = entry["totals"].get(key)
            lines.append(f"{key}: {value:.2f} {currency}" if value is not None else f"{key}: unknown")
        balance = entry["balance"]
        lines.append(f"Confirmed closing: {balance:.2f} {currency}" if balance is not None else "Confirmed closing: unknown")
    if not data.get("currencies"):
        lines.append("Amounts: unknown; source unavailable or coverage unverified")
    lines += ["Free cash: insufficient_data (obligations/reserve not accepted)",
              data.get("formula", "Formula unavailable"), "database_modified=false"]
    return "\n".join(lines)


def render_daily_report(data: dict[str, Any]) -> str:
    return _render(data, "Daily report")


def render_weekly_report(data: dict[str, Any]) -> str:
    return _render(data, "Недельный отчёт")
