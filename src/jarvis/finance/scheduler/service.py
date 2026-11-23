"""APScheduler wrapper for finance-agent background jobs."""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from concurrent.futures import CancelledError
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.finance.scheduler.agent import AgentRuntime

from apscheduler.schedulers.background import (  # type: ignore[import-untyped]
    BackgroundScheduler,
)
from apscheduler.triggers.cron import (  # type: ignore[import-untyped]
    CronTrigger,
)

logger = logging.getLogger(__name__)


class SchedulerService:
    """Manages background jobs."""

    def __init__(self, db_path: str, telegram_sender: Callable[[str], None] | None = None) -> None:
        self.db_path = db_path
        self.telegram_sender = telegram_sender  # callable(text)
        self.scheduler = BackgroundScheduler(timezone="Europe/Moscow")

    def read_only_plan(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Rehearse a saved daily brief without start, registration or delivery."""
        from jarvis.finance.scheduler.reports import dry_run_daily_brief

        return dry_run_daily_brief(self.db_path, now=now)

    def read_only_inventory(self) -> dict[str, Any]:
        """Inspect source registration contracts without starting or registering jobs."""
        from jarvis.finance.scheduler.reports import scheduler_inventory

        return scheduler_inventory()

    async def agent_source_refresh(self, runtime: AgentRuntime, event_key: str, *, deadline: datetime) -> dict[str, Any]:
        """Handle a trusted staging source event without registering jobs or sending."""
        from pathlib import Path
        if Path(self.db_path).resolve() != runtime.journal.path.resolve():
            raise ValueError("Agent event must use this scheduler's staging journal")
        return await runtime.on_source_refresh(event_key, deadline=deadline)

    async def agent_workflow(self, runtime: AgentRuntime, event_key: str, *, deadline: datetime, month: str | None = None) -> dict[str, Any]:
        from pathlib import Path
        if Path(self.db_path).resolve() != runtime.journal.path.resolve():
            raise ValueError("Workflow requires this scheduler's staging journal")
        return await runtime.workflow_event(event_key, deadline=deadline, month=month)

    def start(self) -> None:
        from apscheduler.triggers.interval import (  # type: ignore[import-untyped]
            IntervalTrigger,
        )

        from jarvis.finance.scheduler.jobs import (
            job_anomaly_check,
            job_backup_db,
            job_daily_forecast,
            job_daily_metrics,
            job_daily_report,
            job_drift_check,
            job_forecast_update,
            job_market_scan_morning,
            job_monthly_forecast,
            job_retrain_setfit,
            job_sber_keepalive,
            job_smart_notifications,
            job_sync_sber,
            job_system_health,
            job_weekly_insights,
            job_weekly_report,
        )

        # Sber MCP keepalive every 4 hours
        self.scheduler.add_job(
            self._wrap(job_sber_keepalive, "sber_keepalive"),
            IntervalTrigger(hours=4),
            id="sber_keepalive",
            replace_existing=True,
        )

        # Market scan report daily at 09:00
        self.scheduler.add_job(
            self._wrap(job_market_scan_morning, "market_scan_morning"),
            CronTrigger(hour=9, minute=0),
            id="market_scan_morning",
            replace_existing=True,
        )

        # Daily backup at 03:00
        self.scheduler.add_job(
            self._wrap(job_backup_db, "backup", self.db_path),
            CronTrigger(hour=3, minute=0),
            id="backup_db",
            replace_existing=True,
        )

        # Daily forecast at 08:00
        self.scheduler.add_job(
            self._wrap(job_daily_forecast, "daily_forecast", self.db_path),
            CronTrigger(hour=8, minute=0),
            id="daily_forecast",
            replace_existing=True,
        )

        # Daily system health check at 21:00
        self.scheduler.add_job(
            self._wrap(job_system_health, "system_health", self.db_path),
            CronTrigger(hour=21, minute=0),
            id="system_health",
            replace_existing=True,
        )

        # Weekly insights Sunday 19:00
        self.scheduler.add_job(
            self._wrap(job_weekly_insights, "weekly_insights", self.db_path),
            CronTrigger(day_of_week="sun", hour=19, minute=0),
            id="weekly_insights",
            replace_existing=True,
        )

        # Weekly report Sunday 20:00
        self.scheduler.add_job(
            self._wrap(job_weekly_report, "weekly_report", self.db_path),
            CronTrigger(day_of_week="sun", hour=20, minute=0),
            id="weekly_report",
            replace_existing=True,
        )

        # Monthly forecast on 1st day at 10:00
        self.scheduler.add_job(
            self._wrap(job_monthly_forecast, "monthly_forecast", self.db_path),
            CronTrigger(day=1, hour=10, minute=0),
            id="monthly_forecast",
            replace_existing=True,
        )

        # Nightly SetFit retraining — 00:00
        self.scheduler.add_job(
            self._wrap(job_retrain_setfit, "retrain_setfit", self.db_path),
            CronTrigger(hour=0, minute=0),
            id="retrain_setfit",
            replace_existing=True,
        )

        # Daily drift check — 22:00
        self.scheduler.add_job(
            self._wrap(job_drift_check, "drift_check", self.db_path),
            CronTrigger(hour=22, minute=0),
            id="drift_check",
            replace_existing=True,
        )

        # Hourly smart notifications
        self.scheduler.add_job(
            self._wrap(job_smart_notifications, "smart_notifications", self.db_path),
            CronTrigger(minute=0),
            id="smart_notifications",
            replace_existing=True,
        )

        # Sync Sber MCP every 30 minutes
        self.scheduler.add_job(
            self._wrap(job_sync_sber, "sync_sber", self.db_path),
            IntervalTrigger(minutes=30),
            id="sync_sber",
            replace_existing=True,
        )

        # Daily metrics at 23:00
        self.scheduler.add_job(
            self._wrap(job_daily_metrics, "daily_metrics", self.db_path),
            CronTrigger(hour=23, minute=0),
            id="daily_metrics",
            replace_existing=True,
        )

        # Daily report at 08:30
        self.scheduler.add_job(
            self._wrap(job_daily_report, "daily_report", self.db_path),
            CronTrigger(hour=8, minute=30),
            id="daily_report",
            replace_existing=True,
        )

        # Anomaly check every hour
        self.scheduler.add_job(
            self._wrap(job_anomaly_check, "anomaly_check", self.db_path),
            IntervalTrigger(hours=1),
            id="anomaly_check",
            replace_existing=True,
        )

        # Forecast update at 23:30
        self.scheduler.add_job(
            self._wrap(job_forecast_update, "forecast_update", self.db_path),
            CronTrigger(hour=23, minute=30),
            id="forecast_update",
            replace_existing=True,
        )

        self.scheduler.start()
        logger.info(
            "Scheduler started with jobs: %s",
            [j.id for j in self.scheduler.get_jobs()],
        )

    def _wrap(self, job_func: Callable[..., Any], job_name: str, *args: Any) -> Callable[[], None]:
        """Wrap job to send result to Telegram if sender is set."""

        def _inner() -> None:
            try:
                logger.info("Running scheduler job")
                if self.telegram_sender and job_name in {"weekly_report", "market_scan_morning"}:
                    result = job_func(*args, deliver=False)
                else:
                    result = job_func(*args)
            except (TimeoutError, CancelledError):
                logger.warning("Scheduler job outcome unknown; no automatic notification or replay")
                return
            except Exception as e:  # noqa: BLE001 - contain failures at the scheduled-job boundary
                logger.error("Scheduler job failed; private diagnostics withheld")
                if self.telegram_sender:
                    self._contained_delivery(job_name, f"❌ Job {job_name} failed: {type(e).__name__}")
                return

            logger.info("Scheduler job completed")
            if self.telegram_sender and isinstance(result, str):
                if not (job_name == "drift_check" and result.startswith("DRIFT_OK")):
                    self._contained_delivery(job_name, result)
            elif self.telegram_sender and isinstance(result, dict) and result.get("ok") and job_name == "backup_db":
                self._contained_delivery(job_name, "💾 Бэкап создан")

        return _inner

    def _contained_delivery(self, job_name: str, text: str) -> None:
        """Contain uncertain delivery without sending an error about that delivery."""
        try:
            self._deliver_result(job_name, text)
        except Exception:  # noqa: BLE001 - journal already records unknown, no secondary send
            logger.error("Scheduler delivery outcome unknown; no secondary notification")

    def _deliver_result(self, job_name: str, text: str) -> None:
        """Persist a callback delivery claim before invoking the transport."""
        from jarvis.finance.db import FinanceDB
        from jarvis.finance.scheduler.telegram_notify import (
            claim_delivery,
            finish_delivery,
        )
        if self.telegram_sender is None:
            return
        db = FinanceDB(self.db_path)
        digest = hashlib.sha256(text.encode()).hexdigest()
        key = f"embedded:{job_name}:{datetime.now(UTC).date().isoformat()}:{digest}"
        if claim_delivery(db, key, text) != "claimed":
            return
        delivered = False
        try:
            self.telegram_sender(text)
            delivered = True
        finally:
            finish_delivery(db, key, delivered)

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def list_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for j in self.scheduler.get_jobs():
            jobs.append(
                {
                    "id": j.id,
                    "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                }
            )
        return jobs
