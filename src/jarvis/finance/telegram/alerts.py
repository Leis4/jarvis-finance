import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


class AlertsManager:
    """Manages proactive Telegram alerts with anti-spam protection."""
    
    # Anti-spam: same alert type not repeated more than once per N hours
    COOLDOWN_HOURS: ClassVar[dict[str, int]] = {
        "daily_limit": 6,
        "category_overrun": 24,
        "low_runway": 12,
        "no_activity": 48,
    }
    
    def __init__(self, db: Any, state_file: str = ".alerts_state.json"):
        self.db = db
        self.state_file = Path(state_file)
        self.state = self._load_state()
    
    def _load_state(self) -> dict[str, Any]:
        if self.state_file.exists():
            try:
                res = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(res, dict):
                    return res
                return {}
            except Exception:  # noqa: BLE001
                return {}
        return {}
    
    def _save_state(self) -> None:
        try:
            self.state_file.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Cannot save alerts state: {e}")
    
    def _can_send(self, alert_type: str) -> bool:
        """Check cooldown."""
        last = self.state.get(alert_type, {}).get("last_sent")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
            cooldown = timedelta(hours=self.COOLDOWN_HOURS.get(alert_type, 24))
            return datetime.now(UTC) - last_dt >= cooldown
        except Exception:  # noqa: BLE001
            return True
    
    def _mark_sent(self, alert_type: str, message: str) -> None:
        self.state[alert_type] = {
            "last_sent": datetime.now(UTC).isoformat(),
            "message": message[:200],
        }
        self._save_state()
    
    def check_all(self) -> list[str]:
        """Return list of alert messages to send. Empty if none."""
        alerts = []
        
        # 1. Daily limit
        msg = self._check_daily_limit()
        if msg:
            alerts.append(msg)
        
        # 2. Category overrun
        msg = self._check_category_overrun()
        if msg:
            alerts.append(msg)
        
        # 3. Low cash runway
        msg = self._check_low_runway()
        if msg:
            alerts.append(msg)
        
        # 4. No activity
        msg = self._check_no_activity()
        if msg:
            alerts.append(msg)
        
        return alerts
    
    def _check_daily_limit(self) -> str | None:
        """If today's expenses > 80% of safe_daily_spend, alert."""
        from jarvis.finance.control import TRANSFER_CATEGORIES, build_control_report
        if not self._can_send("daily_limit"):
            return None
        
        try:
            report = build_control_report(self.db)
            safe_daily = float(report.safe_daily_spend)
            if safe_daily <= 0:
                return None
            
            today = datetime.now(UTC).date().isoformat()
            txs = self.db.all_transactions()
            spent_today = sum(
                abs(float(t['amount']))
                for t in txs
                if float(t['amount']) < 0
                and t['tx_date'] == today
                and t['category'] not in TRANSFER_CATEGORIES
            )
            
            if spent_today >= safe_daily * 0.8:
                msg = (
                    f"⚠️ Дневной лимит почти исчерпан.\n"
                    f"Потрачено сегодня: {spent_today:.0f} ₽ / {safe_daily:.0f} ₽"
                )
                self._mark_sent("daily_limit", msg)
                return msg
        except Exception:
            logger.exception("daily_limit check failed")
        return None
    
    def _check_category_overrun(self) -> str | None:
        """If any category > 80% of monthly limit, alert."""
        from jarvis.finance.control import build_control_report
        if not self._can_send("category_overrun"):
            return None
        
        try:
            report = build_control_report(self.db)
            overruns = [
                r for r in report.category_rows
                if float(r.current_limit) > 0 
                and float(r.monthly_spend) > float(r.current_limit) * 0.8
            ]
            if overruns:
                lines = ["🔴 Перерасход категорий:"]
                for r in overruns[:5]:
                    pct = float(r.monthly_spend) / float(r.current_limit) * 100
                    lines.append(f"  {r.category}: {float(r.monthly_spend):.0f} / {float(r.current_limit):.0f} ₽ ({pct:.0f}%)")
                msg = "\n".join(lines)
                self._mark_sent("category_overrun", msg)
                return msg
        except Exception:
            logger.exception("category_overrun check failed")
        return None
    
    def _check_low_runway(self) -> str | None:
        """If cash runway < 3 days, alert."""
        from jarvis.finance.control import build_control_report
        if not self._can_send("low_runway"):
            return None
        
        try:
            report = build_control_report(self.db)
            runway_days = float(report.cash_runway_days)
            if 0 < runway_days < 3:
                msg = (
                    f"⏰ Денег хватит только на {runway_days:.1f} дней.\n"
                    f"Баланс: {float(report.current_balance):.0f} ₽\n"
                    f"При текущем расходе: ~{float(report.estimated_daily_burn):.0f} ₽/день"
                )
                self._mark_sent("low_runway", msg)
                return msg
        except Exception:
            logger.exception("low_runway check failed")
        return None
    
    def _check_no_activity(self) -> str | None:
        """If no new transactions for 24 hours, remind."""
        if not self._can_send("no_activity"):
            return None
        
        try:
            txs = self.db.all_transactions()
            if not txs:
                return None
            latest_date = max(t['tx_date'] for t in txs)
            days_since = (datetime.now(UTC).date() - datetime.fromisoformat(latest_date).date()).days
            if days_since >= 1:
                msg = (
                    f"📝 Не забудь записать сегодняшние траты.\n"
                    f"Последняя транзакция: {latest_date} ({days_since} дн. назад)"
                )
                self._mark_sent("no_activity", msg)
                return msg
        except Exception:
            logger.exception("no_activity check failed")
        return None
