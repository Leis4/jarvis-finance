"""Read-only intelligence job registry and safe dry-run contracts.

No scheduler is started here. Production stays web-only; every cloud job is
disabled by default and dry-run refuses dispatch under --no-jobs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

Trigger = Literal['daily', 'weekdays', 'weekly', 'monthly', 'quarterly', 'first_saturday']


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    title: str
    role_id: str
    model: str
    trigger: Trigger
    hour: int
    minute: int
    day: int = 1
    weekday: int = 6
    enabled: bool = False
    consent_scope: str = 'scheduled_ai'
    purpose: str = 'maintenance'
    ceiling_rub: str = '0.50'
    max_turns: int = 2
    max_tokens: int = 1024


JOBS = (
    JobSpec('daily_observer', 'Local financial observer', 'F06', 'local', 'daily', 8, 30,
            purpose='maintenance', ceiling_rub='0', max_turns=0, max_tokens=0),
    JobSpec('classification', 'Unknown transaction triage', 'F03', 'qwen3.7-flash', 'weekdays', 19, 0,
            consent_scope='category_proposal', purpose='classification'),
    JobSpec('merchant_research', 'Merchant research', 'P02', 'qwen3.7-plus', 'weekly', 20, 0,
            weekday=1, consent_scope='public_research', purpose='research', ceiling_rub='1.00'),
    JobSpec('weekly_review', 'Weekly financial review', 'P03', 'qwen3.7-plus', 'weekly', 19, 0,
            weekday=6, consent_scope='scheduled_plus', purpose='dialogue', ceiling_rub='2.00', max_turns=4),
    JobSpec('memory_consolidation', 'Memory consolidation', 'F08', 'qwen3.7-flash', 'weekly', 19, 30,
            weekday=6, consent_scope='scheduled_ai', purpose='maintenance'),
    JobSpec('monthly_local', 'Monthly local analytics', 'F06', 'local', 'monthly', 10, 0,
            purpose='maintenance', ceiling_rub='0', max_turns=0, max_tokens=0),
    JobSpec('monthly_explanation', 'Monthly explanation', 'P03', 'qwen3.7-plus', 'monthly', 19, 0,
            consent_scope='scheduled_plus', purpose='dialogue', ceiling_rub='2.00', max_turns=4),
    JobSpec('max_strategy', 'Chief financial strategy', 'M01', 'qwen3.7-max', 'monthly', 19, 0, day=2,
            consent_scope='scheduled_max', purpose='reserve', ceiling_rub='6.00', max_turns=6),
    JobSpec('maintenance_review', 'JARVIS maintenance review', 'F08', 'qwen3.7-flash', 'first_saturday', 18, 0,
            purpose='maintenance'),
    JobSpec('quarterly_strategy', 'Long-term strategy', 'M04', 'qwen3.7-max', 'quarterly', 19, 0,
            consent_scope='scheduled_max', purpose='reserve', ceiling_rub='8.00', max_turns=6),
)
REGISTRY = {job.job_id: job for job in JOBS}


def _at(day: datetime, spec: JobSpec) -> datetime:
    return day.replace(hour=spec.hour, minute=spec.minute, second=0, microsecond=0)


def next_run(spec: JobSpec, *, now: datetime, timezone: str = 'Europe/Moscow') -> datetime:
    tz = ZoneInfo(timezone)
    local = now.astimezone(tz)
    candidate = _at(local, spec)
    if spec.trigger == 'daily':
        if candidate <= local:
            candidate += timedelta(days=1)
    elif spec.trigger == 'weekdays':
        while candidate <= local or candidate.weekday() > 4:
            candidate += timedelta(days=1)
            candidate = _at(candidate, spec)
    elif spec.trigger == 'weekly':
        while candidate <= local or candidate.weekday() != spec.weekday:
            candidate += timedelta(days=1)
            candidate = _at(candidate, spec)
    elif spec.trigger == 'monthly':
        while candidate <= local or candidate.day != spec.day:
            year, month = candidate.year, candidate.month + 1
            if month > 12:
                year, month = year + 1, 1
            candidate = _at(candidate.replace(year=year, month=month, day=spec.day), spec)
    elif spec.trigger == 'first_saturday':
        while candidate <= local or candidate.weekday() != 5 or candidate.day > 7:
            candidate += timedelta(days=1)
            candidate = _at(candidate, spec)
    elif spec.trigger == 'quarterly':
        while candidate <= local or candidate.month not in {1, 4, 7, 10} or candidate.day != 1:
            year, month = candidate.year, candidate.month + 1
            if month > 12:
                year, month = year + 1, 1
            candidate = _at(candidate.replace(year=year, month=month, day=spec.day), spec)
    return candidate


def claim(state_dir: Path, job_id: str, idempotency_key: str) -> dict[str, str]:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / (job_id + '.json')
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            return {'status': 'duplicate', **json.loads(path.read_text(encoding='utf-8'))}
        except (OSError, ValueError):
            return {'status': 'duplicate', 'job_id': job_id, 'state': 'UNKNOWN'}
    with os.fdopen(handle, 'w', encoding='utf-8') as saved:
        json.dump({'job_id': job_id, 'idempotency_key': idempotency_key,
                   'state': 'claimed', 'claimed_at': datetime.now(UTC).isoformat()}, saved)
    return {'status': 'claimed', 'job_id': job_id}


def dry_run(spec: JobSpec, *, web_only: bool, consent: bool, fresh_data: bool,
            already_done: bool, expired: bool, budget_rub: Decimal,
            now: datetime | None = None, timezone: str = 'Europe/Moscow') -> dict[str, Any]:
    reason = None
    if web_only:
        reason = 'web_only_no_jobs'
    elif not spec.enabled:
        reason = 'job_disabled'
    elif not consent:
        reason = 'owner_consent_required'
    elif expired:
        reason = 'expired'
    elif already_done:
        reason = 'already_done'
    elif not fresh_data:
        reason = 'no_new_data'
    elif budget_rub < Decimal(spec.ceiling_rub):
        reason = 'insufficient_budget'
    result = {'job_id': spec.job_id, 'role_id': spec.role_id, 'model': spec.model,
              'status': 'SKIPPED' if reason else 'READY', 'skip_reason': reason,
              'ceiling_rub': spec.ceiling_rub,
              'next_run': next_run(spec, now=now or datetime.now(UTC), timezone=timezone).isoformat()}
    return result


LOCAL_JOBS = {'daily_observer', 'monthly_local', 'memory_consolidation'}
CLOUD_JOBS = set(REGISTRY) - LOCAL_JOBS


def scheduler_plan(*, web_only: bool, dispatch_allowed: bool,
                   now: datetime | None = None, timezone: str = 'Europe/Moscow') -> list[dict[str, Any]]:
    """Describe what a future activation would register; never starts jobs."""
    plan = []
    for spec in JOBS:
        handler = 'local' if spec.job_id in LOCAL_JOBS else 'NOT_IMPLEMENTED'
        status = ('SKIPPED' if web_only else 'NOT_ACTIVE' if not dispatch_allowed
                  else 'READY' if handler == 'local' else 'NOT_IMPLEMENTED')
        plan.append({'job_id': spec.job_id, 'handler': handler, 'status': status,
                     'enabled': spec.enabled,
                     'next_run': next_run(spec, now=now or datetime.now(UTC),
                                          timezone=timezone).isoformat()})
    return plan


def install_local_jobs(service: Any, *, cfo: Any, store: Any, web_only: bool,
                       dispatch_allowed: bool, timezone: str = 'Europe/Moscow') -> dict[str, Any]:
    """Register only implemented local jobs; cloud jobs stay unregistered."""
    if web_only or not dispatch_allowed:
        return {'status': 'NOT_ACTIVE', 'registered': [], 'reason': 'dispatch_not_allowed'}
    from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
    registered = []
    for spec in JOBS:
        if spec.job_id not in LOCAL_JOBS:
            continue
        trigger = CronTrigger(hour=spec.hour, minute=spec.minute,
                              day=spec.day if spec.trigger == 'monthly' else None,
                              day_of_week=spec.weekday if spec.trigger in {'weekly', 'first_saturday'} else None,
                              timezone=timezone)
        service.scheduler.add_job(
            lambda job_id=spec.job_id, enabled=spec.enabled: run_job(job_id, cfo=cfo, store=store,
                web_only=web_only, enabled=enabled, dispatch_allowed=dispatch_allowed),
            trigger, id='intelligence_' + spec.job_id, replace_existing=True)
        registered.append(spec.job_id)
    return {'status': 'REGISTERED', 'registered': registered,
            'not_implemented': sorted(CLOUD_JOBS), 'financial_apply': False}


def run_job(job_id: str, *, cfo: Any, store: Any, web_only: bool, enabled: bool,
            dispatch_allowed: bool, now: datetime | None = None) -> dict[str, Any]:
    """Local handlers only; cloud jobs stay NOT_IMPLEMENTED without dispatch."""
    if job_id not in REGISTRY:
        raise ValueError('Unknown job')
    if web_only:
        return {'job_id': job_id, 'status': 'SKIPPED', 'reason': 'web_only_no_jobs'}
    if not enabled:
        return {'job_id': job_id, 'status': 'SKIPPED', 'reason': 'job_disabled'}
    if not dispatch_allowed:
        return {'job_id': job_id, 'status': 'NOT_ACTIVE', 'reason': 'dispatch_not_allowed'}
    if job_id == 'daily_observer':
        from jarvis.finance.personal_briefing import daily_briefing
        return {'job_id': job_id, 'status': 'COMPLETED', 'result': daily_briefing(cfo),
                'financial_apply': False}
    if job_id == 'monthly_local':
        from jarvis.finance.monthly_analysis import monthly_report
        return {'job_id': job_id, 'status': 'COMPLETED',
                'result': monthly_report(cfo, (now or datetime.now(UTC)).date()),
                'financial_apply': False}
    if job_id == 'memory_consolidation':
        from jarvis.services.insights import collect
        return {'job_id': job_id, 'status': 'COMPLETED', 'result': collect(cfo, store),
                'financial_apply': False}
    return {'job_id': job_id, 'status': 'NOT_IMPLEMENTED',
            'reason': 'cloud_handler_not_wired', 'financial_apply': False}


def telemetry(*, web_only: bool, timezone: str = 'Europe/Moscow',
              now: datetime | None = None) -> dict[str, object]:
    return {'web_only': web_only, 'timezone': timezone,
            'jobs': [{'job_id': spec.job_id, 'title': spec.title, 'role_id': spec.role_id,
                      'model': spec.model, 'enabled': spec.enabled, 'consent_scope': spec.consent_scope,
                      'purpose': spec.purpose, 'ceiling_rub': spec.ceiling_rub,
                      'max_turns': spec.max_turns,
                      'next_run': next_run(spec, now=now or datetime.now(UTC), timezone=timezone).isoformat()}
                     for spec in JOBS]}
