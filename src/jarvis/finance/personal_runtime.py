"""Bounded personal jobs on the existing scheduler, with no automatic PIN retries."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock
from typing import Any
from zoneinfo import ZoneInfo

from jarvis.finance.personal_cfo import PersonalCFO, backup_database
from jarvis.finance.personal_product import RuntimeSettings
from jarvis.finance.personal_recovery import verify_backup
from jarvis.finance.personal_telegram import PersonalTelegram
from jarvis.finance.scheduler.service import SchedulerService
from jarvis.finance.telegram.bot import TelegramConfig


def state(cfo: PersonalCFO) -> dict[str, Any]:
    jobs = cfo.db.get_setting('cfo.jobs', {})
    heartbeat = jobs.get('heartbeat', {}).get('at')
    current = datetime.now(UTC)
    try:
        active = bool(jobs.get('heartbeat', {}).get('status') == 'running' and heartbeat and timedelta(0) <= current-datetime.fromisoformat(heartbeat) <= timedelta(minutes=3))
    except (ValueError, TypeError):
        active = False
    return {'settings': RuntimeSettings.model_validate(cfo.db.get_setting('cfo.runtime_settings', {})).model_dump(),
        'jobs': jobs, 'runtime_active': active, 'telegram': cfo.db.get_setting('cfo.telegram_state', {'status': 'not_started'}),
        'autostart': cfo.db.get_setting('cfo.autostart', {'status': 'not_configured'}),
        'quiet_hours': '23:00–08:00 Europe/Moscow', 'host_required': 'Computer must remain on; no off-PC 24/7 claim'}


class PersonalRuntime:
    """Reuse one CFO instance, one scheduler executor and one owner Telegram adapter."""

    def __init__(self, cfo: PersonalCFO, workspace: Path, telegram: PersonalTelegram | None = None):
        self.cfo, self.workspace = cfo, workspace
        self.telegram = telegram or PersonalTelegram(cfo, TelegramConfig.from_env())
        self.service = SchedulerService(str(cfo.db.path))
        self._tick_lock = Lock()
        self._stopping = Event()

    def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Serialize even direct invocations; persist a heartbeat before slow work."""
        if not self._tick_lock.acquire(blocking=False):
            return {'status': 'busy'}
        try:
            return self._tick(now=now)
        finally:
            self._tick_lock.release()

    def _tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        clock = now or datetime.now(UTC)
        if clock.tzinfo is None:
            raise ValueError('Aware runtime timestamp required')
        if self._stopping.is_set():
            return {'status': 'stopped'}
        settings = RuntimeSettings.model_validate(self.cfo.db.get_setting('cfo.runtime_settings', {}))
        jobs: dict[str, Any] = self.cfo.db.get_setting('cfo.jobs', {})
        jobs['heartbeat'] = {'status': 'running', 'at': clock.isoformat()}
        self.cfo.db.set_setting('cfo.jobs', jobs)
        today = clock.date().isoformat()
        daily_directory = self.workspace/'backups'/'daily'
        backup_file = str(jobs.get('backup', {}).get('file', ''))
        verified = False
        if self.cfo.db.get_setting('cfo.daily_backup', None) == today and backup_file and Path(backup_file).name == backup_file:
            try:
                verify_backup(daily_directory/backup_file)
                verified = True
            except (OSError, ValueError, KeyError, sqlite3.Error):
                pass  # Missing/corrupt copy must be replaced before further sync.
        if not verified:
            try:
                target = backup_database(self.cfo.db, daily_directory)
                self.cfo.db.set_setting('cfo.daily_backup', today)
                jobs['backup'] = {'status': 'verified', 'at': clock.isoformat(), 'file': target.name}
            except (OSError, ValueError, sqlite3.Error):
                jobs['backup'] = {'status': 'unavailable', 'at': clock.isoformat()}
                self.alert(settings, clock, 'backup:'+today, 'Не удалось проверить резервную копию. Фоновое обновление остановлено до исправления.')
                self.cfo.db.set_setting('cfo.jobs', jobs)
                return jobs  # Never update real data without the required backup.
        previous = jobs.get('sync', {})
        next_at = datetime.fromisoformat(previous['next_at']) if previous.get('next_at') else None
        snapshot = self.cfo.bank_snapshot()
        observed = datetime.fromisoformat(snapshot['observed_at']) if snapshot else clock-timedelta(days=1)
        due = observed + timedelta(minutes=settings.sync_minutes)
        held_for_owner = previous.get('status') == 'auth_required' and previous.get('capture_sha256') == (snapshot or {}).get('capture_sha256')
        if settings.bank_enabled and not held_for_owner and (next_at is None or next_at <= clock) and due <= clock:
            before = {r['id'] for r in self.cfo.db.all_transactions()}
            try:
                result = self.cfo.refresh(allow_pin=False)
                if result['status'] == 'updated':
                    jobs['sync'] = {'status': 'updated', 'at': clock.isoformat(), 'inserted': result['inserted'],
                        'next_at': (clock+timedelta(minutes=settings.sync_minutes)).isoformat(), 'errors': 0}
                    new = [r for r in self.cfo.db.all_transactions() if r['id'] not in before and r['category'] in {'unknown','pending_review','uncategorized'}]
                    count = 0
                    classification: dict[str, Any] = {'status': 'review_only', 'at': clock.isoformat(),
                        'financial_apply': False}
                    self.cfo.db.set_setting('cfo.jobs', jobs)
                    from jarvis.finance.classification.intelligence import (
                        propose,
                        screened,
                    )
                    for row in new[:3]:
                        try:
                            from jarvis.finance.classification.learning import (
                                neighbor_proposal,
                            )
                            proposal = neighbor_proposal(self.cfo, row['id'])
                            if proposal is None:
                                if not settings.paid_classification_enabled:
                                    continue
                                proposal = asyncio.run(propose(screened(row['description'], self._amount(row['amount']))))
                        except Exception as exc:  # noqa: BLE001 - bank sync has already committed
                            classification.update(status='unavailable', error_type=type(exc).__name__)
                            break
                        self.cfo.db.set_setting('cfo.ai_proposal:'+str(row['id']), proposal)
                        count += 1
                        if proposal.get('status') == 'unknown':
                            break
                    classification.update(proposals=count, remaining_for_review=max(0,len(new)-count))
                    jobs['classification'] = classification
                else:
                    failures = min(6, int(previous.get('errors',0))+1)
                    jobs['sync'] = {'status': result['status'], 'code': result.get('code'), 'errors': failures,
                        'at': clock.isoformat(), 'next_at': (clock+timedelta(seconds=min(3600,60*2**failures))).isoformat(),
                        'capture_sha256': (snapshot or {}).get('capture_sha256'),
                        'owner_action_required': result['status'] == 'auth_required'}
            except Exception as exc:  # noqa: BLE001 - jobs persist safe failure types, not bank payloads
                jobs['sync'] = {'status': 'unavailable', 'error_type': type(exc).__name__,
                    'at': clock.isoformat(), 'next_at': (clock+timedelta(minutes=30)).isoformat(), 'errors': 1}
        local = clock.astimezone(ZoneInfo('Europe/Moscow'))
        day = local.date().isoformat()
        daily = jobs.get('daily', {})
        if settings.daily_enabled and 8 <= local.hour < 23 and local.hour >= settings.daily_hour and daily.get('day') != day:
            delivered = self.telegram.deliver_summary('daily:'+day)
            if delivered['status'] in {'delivered','unknown','not_repeated','failed'}:
                jobs['daily'] = {**delivered, 'day': day, 'at': clock.isoformat()}
        week = local.strftime('%G-W%V')
        if (settings.weekly_enabled and local.weekday() == 0 and settings.weekly_hour <= local.hour < 23
                and jobs.get('weekly', {}).get('week') != week):
            receipt = self.telegram.deliver('weekly:'+week, self.telegram.weekly_summary())
            if receipt['status'] != 'unavailable':
                jobs['weekly'] = {**receipt, 'week': week, 'at': clock.isoformat()}
        sync = jobs.get('sync', {})
        if sync.get('status') == 'auth_required':
            self.notify_owner(settings, clock, 'auth:'+str(sync.get('capture_sha256')),
                'Требуется авторизация Сбер: получен код pin_required. '
                'Отправьте /pin 12345 в этот чат (5 цифр) или обновите Сбер в http://127.0.0.1:8766/.',
                critical=True)
        elif sync.get('status') == 'unavailable':
            self.alert(settings, clock, 'sync:'+day, 'Фоновое обновление банка недоступно. Последнее наблюдение может устареть.')
        elif jobs.get('classification', {}).get('remaining_for_review', 0):
            self.alert(settings, clock, 'review:'+str(sync.get('at')), 'Есть новые операции с неясным назначением. /review — проверить; суммы не изменены.')
        if self._stopping.is_set():
            jobs['heartbeat'] = {'status': 'stopped', 'at': datetime.now(UTC).isoformat()}
        self.cfo.db.set_setting('cfo.jobs', jobs)
        return jobs

    def notify_owner(self, settings: RuntimeSettings, clock: datetime, event: str,
                     text: str, *, critical: bool = False) -> None:
        """Critical auth notice ignores the optional informational-alert flag."""
        if not critical and not settings.alerts_enabled:
            return
        if not 8 <= clock.astimezone(ZoneInfo('Europe/Moscow')).hour < 23:
            return
        receipt = self.telegram.deliver('alert:'+event, text)
        if not critical and receipt['status'] not in {'unavailable', 'not_repeated'}:
            self.cfo.db.set_setting('cfo.alert_last', clock.isoformat())

    def alert(self, settings: RuntimeSettings, clock: datetime, event: str, text: str) -> None:
        """Optional safe alerts: quiet hours, at most one attempt per hour, no replay."""
        if not settings.alerts_enabled or not 8 <= clock.astimezone(ZoneInfo('Europe/Moscow')).hour < 23:
            return
        previous = self.cfo.db.get_setting('cfo.alert_last', None)
        if previous and clock-datetime.fromisoformat(previous) < timedelta(hours=1):
            return
        receipt = self.telegram.deliver('alert:'+event, text)
        if receipt['status'] not in {'unavailable', 'not_repeated'}:
            self.cfo.db.set_setting('cfo.alert_last', clock.isoformat())

    @staticmethod
    def _amount(value: str) -> Decimal:
        from jarvis.finance.personal_cfo import money
        return money(value)

    def start(self) -> None:
        from apscheduler.executors.pool import (  # type: ignore[import-untyped]
            ThreadPoolExecutor,
        )
        self.telegram.start()
        self.service.scheduler.configure(executors={'default': ThreadPoolExecutor(1)},
            job_defaults={'max_instances': 1, 'coalesce': True, 'misfire_grace_time': 120})
        self.service.scheduler.add_job(self.tick, 'interval', seconds=60, id='personal_cfo_tick',
            next_run_time=datetime.now(UTC)+timedelta(seconds=20))
        self.service.scheduler.start()

    def stop(self) -> None:
        self._stopping.set()
        self.telegram.stop()
        if self.service.scheduler.running:
            self.service.scheduler.shutdown(wait=False)
        jobs = self.cfo.db.get_setting('cfo.jobs', {})
        jobs['heartbeat'] = {'status': 'stopped', 'at': datetime.now(UTC).isoformat()}
        self.cfo.db.set_setting('cfo.jobs', jobs)
