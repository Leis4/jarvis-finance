"""Background jobs for finance-agent. Run on schedule."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import CancelledError
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.event_taxonomy import canonical_category

if TYPE_CHECKING:
    from jarvis.finance.scheduler.telegram_notify import TelegramNotifier

logger = logging.getLogger(__name__)


def _safe_run(
    fn: Callable[..., dict[str, Any] | str | None],
    *args: Any,
    max_retries: int = 3,
    replay_safe: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run fn with retries; never raise. Returns dict with status."""
    if type(max_retries) is not int or not 1 <= max_retries <= 10:
        raise ValueError("Attempt limit must be an integer from 1 to 10")
    if type(replay_safe) is not bool:
        raise ValueError("Replay policy must be explicit boolean")
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and result.get("status") == "ok":
                return result
            if isinstance(result, dict):
                logger.warning(
                    "job returned non-ok status=%s",
                    _diagnostic_status(result.get("status")),
                )
                return result
            return {"status": "ok", "result": result}
        except (TimeoutError, CancelledError) as interrupted:
            logger.warning("job interrupted; effect unknown, no automatic replay")
            return {"status": "unknown", "error": type(interrupted).__name__, "attempts": attempt, "replay_allowed": False}
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("job failed (attempt %d/%d)", attempt, max_retries)
            if not replay_safe:
                return {"status": "error", "error": type(exc).__name__, "attempts": attempt,
                        "replay_allowed": False}
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))
    logger.error("job failed after %d attempts", max_retries)
    return {"status": "error", "error": type(last_exc).__name__ if last_exc else "unknown", "attempts": max_retries}


def _diagnostic_status(value: object) -> str:
    """Emit a fixed status vocabulary, never stringify job-controlled payloads."""
    allowed = {"ok", "available", "stale", "unavailable", "blocked", "insufficient_data",
               "unknown", "error", "pending_review", "experimental"}
    return value if type(value) is str and value in allowed else "unspecified"




def job_backup_db(db_path: str, backup_dir: str = "backups") -> dict[str, Any]:
    """Publish only a verified archive that restores into a disposable path."""
    from jarvis.finance.backup import create_backup, restore_backup

    destination = Path(backup_dir)
    destination.mkdir(parents=True, exist_ok=True)
    src = Path(db_path)
    if not src.exists():
        return {"ok": False, "status": "unavailable", "reason": "db_not_found",
                "database_modified": False}

    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-scheduled-backup-", dir=destination) as temporary:
            staging = Path(temporary)
            result = create_backup(src, staging)
            restored = restore_backup(result.path, staging / "rehearsal" / "restored.db")
            archive_sha256 = hashlib.sha256(result.path.read_bytes()).hexdigest()
            restored_sha256 = hashlib.sha256(restored.read_bytes()).hexdigest()
            published = destination / result.path.name
            shutil.move(str(result.path), published)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        logger.warning("verified backup unavailable: %s", type(exc).__name__)
        return {"ok": False, "status": "unavailable", "reason": type(exc).__name__,
                "database_modified": False}

    backups = sorted(
        destination.glob("finance-agent-backup-*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[30:]:
        try:
            old.unlink()
        except Exception:  # noqa: BLE001, S110
            pass

    return {"ok": True, "status": "verified", "path": str(published), "total": min(len(backups), 30),
            "archive_sha256": archive_sha256, "restored_database_sha256": restored_sha256,
            "recovery_rehearsed": True, "database_modified": False}


def job_weekly_report(db_path: str, notifier: TelegramNotifier | None = None, deliver: bool = True) -> str:
    """Weekly report text for Telegram."""
    from jarvis.finance.scheduler.reports import (
        build_weekly_report_data,
        render_weekly_report,
    )
    from jarvis.finance.scheduler.telegram_notify import TelegramNotifier
    data = build_weekly_report_data(db_path)
    text = render_weekly_report(data)
    if deliver:
        from jarvis.finance.scheduler.telegram_notify import send_once
        send_once(FinanceDB(db_path), notifier or TelegramNotifier(),
                  f"weekly_report:{datetime.now(UTC).date().isoformat()}", text)
    return text



def job_monthly_forecast(db_path: str) -> str:
    """Monthly plan for 1st day of month."""
    return _audited_forecast_text(db_path, 30)


def _audited_forecast(db_path: str, horizon: int = 14) -> dict[str, Any]:
    """Forecast only a reconciled complete period ending yesterday."""
    from jarvis.finance.audit_report import build_audit_report, liquidity_forecast
    from jarvis.finance.ledger import cashflow
    db = FinanceDB(db_path)
    end = datetime.now(UTC).date() - timedelta(days=1)
    start = end - timedelta(days=27)
    report = build_audit_report(db, start, end)
    result = report["forecast"]
    if result["status"] != "experimental":
        return {**result, "asof": end.isoformat()}
    rows = db.transactions_between(start, end)
    daily = [float(cashflow([row for row in rows if row["tx_date"] == (start + timedelta(days=i)).isoformat()])["movement"])
             for i in range(28)]
    closing = sum((Decimal(account["reconciliation"]["closing"]) for account in report["accounts"]), Decimal(0))
    return {**liquidity_forecast(daily, closing, horizon=horizon), "asof": end.isoformat()}


def _audited_forecast_text(db_path: str, horizon: int) -> str:
    result = _audited_forecast(db_path, horizon)
    if result["status"] == "insufficient_data":
        return "Прогноз недоступен: нужны 28 дней подтверждённого покрытия и сверенный остаток; см. /audit."
    return "Экспериментальный прогноз, будущие обязательства показаны отдельно:\n" + json.dumps(result, ensure_ascii=False)


def job_retrain_setfit(db_path: str) -> str:
    """Weekly SetFit retraining. Returns Telegram-friendly message."""
    from jarvis.finance.ml_local.retrain import (  # type: ignore[import-untyped]
        retrain_setfit,
    )

    try:
        result = retrain_setfit(db_path)
        action = result.get("action")

        if action == "promoted":
            return (
                "🧠 SetFit переобучен\n"
                f"   f1_macro: {result['old_f1_macro']:.3f} → {result['new_f1_macro']:.3f}\n"
                f"   Архив: {result['archived_to']}"
            )
        if action == "rejected":
            return f"🧠 SetFit переобучение отклонено\n   {result['reason']}"
        if action == "skipped":
            return f"🧠 SetFit пропущено: {result['reason']}"
        return f"🧠 SetFit ошибка: {result.get('reason', 'unknown')}"
    except Exception as e:
        logger.exception("retrain_setfit failed")
        return f"❌ SetFit retrain failed: {e}"


def job_drift_check(db_path: str) -> str:
    """Daily check for model drift and auto-trigger retrain if detected."""
    from jarvis.finance.ml_local.drift_detector import detect_drift
    from jarvis.finance.ml_local.retrain import retrain_setfit

    db = FinanceDB(db_path)
    report = detect_drift(db, window_days=7)

    if not report.drift_detected:
        return f"DRIFT_OK: predictions={report.total_predictions}, avg_conf={report.avg_confidence:.2f}"

    last_retrain = db.get_setting("last_drift_retrain_time", None)
    if last_retrain:
        try:
            last_dt = datetime.fromisoformat(last_retrain)
            if datetime.now(UTC) - last_dt < timedelta(hours=24):
                return "⚠️ Дрейф обнаружен, но ретрейн уже запускался менее 24 часов назад."
        except Exception:  # noqa: BLE001, S110
            pass

    db.set_setting("last_drift_retrain_time", datetime.now(UTC).isoformat())

    reasons_str = "\n".join(f"- {r}" for r in report.reasons)
    intro = f"⚠️ Обнаружен дрейф модели:\n{reasons_str}\n\nЗапускаю retrain..."

    try:
        res = retrain_setfit(db_path)
        action = res.get("action")
        old_f1 = float(res.get("old_f1_macro", 0.0))
        new_f1 = float(res.get("new_f1_macro", 0.0))

        if action == "promoted":
            return (
                f"{intro}\n\n"
                f"✅ Retrain завершён. Новая f1_macro={new_f1:.2f} (было {old_f1:.2f}). Модель promoted."
            )
        if action == "rejected":
            return (
                f"{intro}\n\n"
                f"❌ Retrain отклонён: новая f1_macro={new_f1:.2f} <= {old_f1:.2f}. Оставляю старую модель."
            )
        return f"{intro}\n\n⚠️ Retrain статус: {res.get('reason', 'unknown')}"
    except Exception as e:
        logger.exception("Drift retrain failed")
        return f"{intro}\n\n❌ Retrain error: {e}"


def job_daily_forecast(db_path: str) -> str:
    """Daily cash flow forecast at 08:00."""
    return _audited_forecast_text(db_path, 14)


def job_system_health(db_path: str, log_dir: str = "logs", chat_id: int | str | None = None) -> None:
    """Daily system health check at 21:00."""
    from jarvis.finance.scheduler.telegram_notify import TelegramNotifier, send_once
    from jarvis.finance.supervisor.reporter import build_report, render_health_report

    report = build_report(db_path, log_dir)

    text = render_health_report(report, include_ok=False)

    target_chat_id = chat_id or os.getenv("TELEGRAM_ADMIN_CHAT_ID")
    if not target_chat_id:
        return

    send_once(FinanceDB(db_path), TelegramNotifier(chat_id=target_chat_id),
              f"system_health:{datetime.now(UTC).date().isoformat()}", text)


def job_weekly_insights(db_path: str, chat_id: int | str | None = None) -> None:
    """Weekly insights — Sunday 19:00."""
    import asyncio

    from jarvis.finance.insights.llm_advisor import get_advice
    from jarvis.finance.insights.reporter import build_insight_report, render_insights
    from jarvis.finance.scheduler.telegram_notify import TelegramNotifier, send_once

    report = build_insight_report(db_path, period_days=30)
    if report.insights:
        try:
            report.llm_summary = asyncio.run(get_advice(report.insights))
        except Exception:
            logger.exception("LLM advisor failed")

    text = render_insights(report, format_mode="telegram")

    target_chat_id = chat_id or os.getenv("TELEGRAM_ADMIN_CHAT_ID")
    if not target_chat_id:
        return
    send_once(FinanceDB(db_path), TelegramNotifier(chat_id=target_chat_id),
              f"weekly_insights:{datetime.now(UTC).date().isoformat()}", text)


def job_smart_notifications(db_path: str, chat_id: int | None = None) -> None:
    """Hourly smart notifications — fires at HH:00."""
    _log = logging.getLogger(__name__)
    if chat_id is None:
        chat_id_env = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
        if not chat_id_env:
            _log.warning("TELEGRAM_ADMIN_CHAT_ID not set — skipping notifications")
            return
        chat_id = int(chat_id_env)
    try:
        from jarvis.finance.notifications.dispatcher import (
            dispatch_sync,
        )

        n = dispatch_sync(db_path, chat_id)
        _log.info("Smart notifications sent: %d", n)
    except Exception:  # noqa: BLE001
        _log.exception("job_smart_notifications failed")


_PERSON_NAME_RE = re.compile(r"^[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.?\s*$")
_OWN_NAMES = ("пётр николайович",)
_FAMILY_NAMES = ("ольга николаевна", "николай николаевич", "сергей николайович")
_FRIEND_REIMBURSEMENT_NAMES = ("александр александрович м.", "александр александрович м")
_POSITIVE_ONLY = {
    "compensation", "investment_income",
    "gift_in", "cash_deposit", "salary",
}


def _looks_like_person(op: dict[str, Any]) -> bool:
    """True if merchant looks like 'Имя Отчество И.'"""
    m = str(op.get("merchant", "")).strip()
    return bool(_PERSON_NAME_RE.match(m))


def _person_category(op: dict[str, Any]) -> str | None:
    """self->transfer, family->family_income, friend->compensation/peer_transfer_out, peer->peer_transfer_in/out."""
    m = str(op.get("merchant", "")).strip().lower()
    amt_val = op.get("amount", 0)
    if isinstance(amt_val, dict):
        amt_val = amt_val.get("amount", 0)
    try:
        amount = float(str(amt_val or 0))
    except (TypeError, ValueError):
        amount = 0.0

    if any(n in m for n in _OWN_NAMES):
        return "pending_review"
    if any(n in m for n in _FRIEND_REIMBURSEMENT_NAMES):
        return "compensation" if amount > 0 else "peer_transfer_out"
    if any(n in m for n in _FAMILY_NAMES):
        return "family_income"
    if _looks_like_person(op):
        return "peer_transfer_in" if amount > 0 else "peer_transfer_out"
    return None


def _fallback_for_negative(op: dict[str, Any]) -> str:
    """Wrongly positive-only negative tx: best guess."""
    if _looks_like_person(op):
        return "peer_transfer_out"
    return "uncategorized"


def _detect_special_category(
    op: dict[str, Any], *, confirmed_owned_account_ids: frozenset[str] = frozenset(),
    confirmed_investment_category: str | None = None,
) -> str | None:
    """Suggest categories; confirmed investment context must come from review, not raw data."""
    if confirmed_investment_category is not None:
        if confirmed_investment_category not in {
            "investment_principal", "investment_purchase", "investment_sale",
            "investment_dividend", "investment_coupon", "investment_realized_profit", "bank_fees",
        }:
            raise ValueError("Unsupported confirmed investment category")
        return confirmed_investment_category
    p_cat = _person_category(op)
    if _is_internal_transfer(op, confirmed_owned_account_ids=confirmed_owned_account_ids):
        cat = "transfer"
    elif p_cat:
        cat = p_cat
    else:
        merchant = str(op.get("merchant") or "").lower()
        description = str(op.get("description") or "").lower()
        combined = f"{merchant} {description}".strip()

        _INVESTMENT_BUY_MARKERS = (
            "покупка биржевых паев", "покупка ценных бумаг",
            "покупка акций", "покупка облигаций", "покупка фонда",
        )
        _INVESTMENT_TRANSFER_MARKERS = (
            "автопополнение брокерского", "пополнение брокерского",
            "перевод на брокерский", "вывод с брокерского",
            "зачисление с брокерского",
        )

        if any(k in combined for k in _INVESTMENT_BUY_MARKERS):
            cat = "investment_buy"
        elif any(k in combined for k in _INVESTMENT_TRANSFER_MARKERS):
            amt_val = op.get("amount", 0)
            if isinstance(amt_val, dict):
                amt_val = amt_val.get("amount", 0)
            try:
                amt_num = float(str(amt_val or 0))
            except (TypeError, ValueError):
                amt_num = 0.0
            if amt_num > 0:
                cat = "pending_review"
            else:
                cat = "investment_transfer"
        elif (
            "дебетовые карты" in combined
            or "дебетовая карта" in combined
            or "фонд сберегательный" in combined
            or "счёт 422" in combined
            or "счет 422" in combined
        ):
            cat = "pending_review"
        elif "за услугу" in combined and ("уведомлен" in combined or "уведомлен" in description or "мобильный банк" in combined):
            cat = "bank_fees"
        else:
            cash_in_markers = (
                "внесение наличных",
                "пополнение через банкомат",
                "внесение через",
            )
            cash_out_markers = (
                "снятие наличных",
                "выдача наличных",
                "банкомат",
                "atm ",
            )
            if any(m in combined for m in cash_in_markers):
                cat = "cash_deposit"
            elif any(m in combined for m in cash_out_markers):
                cat = "cash_withdrawal"
            else:
                cat = None

    if cat in _POSITIVE_ONLY:
        amt_val = op.get("amount", 0)
        if isinstance(amt_val, dict):
            amt_val = amt_val.get("amount", 0)
        try:
            amount = float(str(amt_val or 0))
        except (TypeError, ValueError):
            amount = 0.0
        if amount < 0:
            cat = _fallback_for_negative(op)

    return canonical_category(cat) if cat else None


def _is_internal_transfer(
    op: dict[str, Any], *, confirmed_owned_account_ids: frozenset[str] = frozenset(),
) -> bool:
    """Require distinct endpoints in an independently confirmed account register."""
    to_res = op.get("to_resource")
    from_res = op.get("from_resource")
    return (
        isinstance(to_res, dict)
        and isinstance(from_res, dict)
        and to_res.get("id") != from_res.get("id")
        and str(to_res.get("id")) in confirmed_owned_account_ids
        and str(from_res.get("id")) in confirmed_owned_account_ids
    )


def _classify_new_transaction(db: FinanceDB, external_id: str, db_path: str) -> str | None:
    """Run classification on newly inserted transaction, update category."""
    tx_dict = db.find_transaction_by_external_id(external_id) if hasattr(db, "find_transaction_by_external_id") else None
    if not tx_dict:
        txs = [t for t in db.all_transactions() if t.get("external_id") == external_id]
        if not txs:
            return None
        tx_dict = txs[0]

    tx_id = int(tx_dict["id"])
    merchant = str(tx_dict.get("merchant") or "")
    description = str(tx_dict.get("description") or "")
    desc = f"{merchant} {description}".strip()
    try:
        amount = float(str(tx_dict.get("amount", 0)))
    except (TypeError, ValueError):
        amount = 0.0

    try:
        from jarvis.finance.classification.layers import (
            l0_preprocess,
            l1_hard_rules,
            l2_learned_patterns,
            l3_historical_vote,
            l5_merchant_kb,
        )

        ctx = l0_preprocess(desc, amount)
        res = l1_hard_rules(db_path, ctx) or l2_learned_patterns(db_path, ctx) or l3_historical_vote(db_path, ctx) or l5_merchant_kb(ctx)
        category = res.category if res else None
    except Exception as e:  # noqa: BLE001
        logger.debug("classification failed for %s: %s", external_id, e)
        return None

    if category and category != "uncategorized":
        db.update_transaction_category(tx_id, category, source="sber_sync")
        return category
    return None


def _insert_op_safe(db: FinanceDB, op: dict[str, Any], db_path: str = "data/jarvis.db") -> bool:
    """Insert one Sber operation into DB if not existing. Returns True if inserted."""
    op_id = str(op.get("id", ""))
    if not op_id:
        raise ValueError("Sber operation has no external ID")

    amt = op.get("amount", {})
    try:
        amount = Decimal(str(amt["amount"])) if isinstance(amt, dict) else Decimal(str(amt))
        if not amount.is_finite():
            raise ValueError("Non-finite Sber amount")
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid Sber operation amount") from exc

    date_str = str(op.get("date", ""))[:10]
    merchant = str(op.get("merchant") or op.get("description") or "sber")
    description = str(op.get("description") or "")
    account_id = str(op.get("_account_id") or "sber")
    from_res = op.get("from_resource")
    if not op.get("_account_id") and isinstance(from_res, dict) and from_res.get("id"):
        account_id = str(from_res["id"])

    special = _detect_special_category(op)
    initial_category = special if special else "uncategorized"

    from jarvis.finance.domain import Transaction
    tx = Transaction(
        tx_date=datetime.fromisoformat(date_str).date() if "T" in date_str else datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).date(),
        amount=amount,
        currency=str(amt.get("currency", "RUB")) if isinstance(amt, dict) else "RUB",
        description=description,
        merchant=merchant,
        category=initial_category,
        account=account_id,
        external_id=op_id,
    )
    inserted = db.add_transaction(tx, source="sber_mcp") > 0
    if inserted and initial_category in ("uncategorized", ""):
        _classify_new_transaction(db, op_id, db_path)
    return inserted


def job_sync_sber(db_path: str, notifier: TelegramNotifier | None = None) -> dict[str, Any]:
    """Poll Sber MCP, import new operations into DB."""
    from jarvis.finance.scheduler.telegram_notify import TelegramNotifier  # noqa: F401
    from jarvis.finance.source_health import record_source_state
    from jarvis.services.invest_service import InvestService

    db = FinanceDB(db_path)
    service = InvestService()

    if not service.sber_configured():
        logger.warning("job_sync_sber: Sber MCP not configured")
        record_source_state(db, "sber_mcp", "unavailable")
        return {"status": "skipped", "reason": "sber not configured"}

    try:
        today = datetime.now(UTC).date()
        from_date = (today - timedelta(days=3)).isoformat()
        to_date = today.isoformat()
        ops = service.get_operations(from_date, to_date, limit=100, truncate=False)
    except Exception as e:
        logger.exception("job_sync_sber failed to fetch")
        record_source_state(db, "sber_mcp", "error", error_type=type(e).__name__)
        return {"status": "error", "message": type(e).__name__}

    new_count = 0
    rejected = 0
    for op in ops:
        try:
            if _insert_op_safe(db, op, db_path=db_path):
                new_count += 1
        except (ValueError, InvalidOperation):
            rejected += 1
    record_source_state(db, "sber_mcp", "error" if rejected else "ok", coverage_end=to_date,
                        error_type="InvalidOperations" if rejected else None)

    logger.info("job_sync_sber: %d new operations imported", new_count)
    return {"status": "error" if rejected else "ok", "imported": new_count, "total_fetched": len(ops), "rejected": rejected}


def job_daily_metrics(db_path: str) -> dict[str, Any]:
    """Compute and persist daily analytics."""
    from jarvis.finance.analytics import (
        detect_recurring_subscriptions,
        detect_waste_hotspots,
    )

    db = FinanceDB(db_path)
    result: dict[str, Any] = {"status": "ok", "source_errors": {}, "scope": "observed_analytics_not_account_completeness"}

    try:
        subs = detect_recurring_subscriptions(db)
        result["subscriptions"] = len(subs)
    except Exception as exc:  # noqa: BLE001 - analytics source boundary
        logger.warning("analytics subscriptions failed: %s", type(exc).__name__)
        result["subscriptions"] = None
        result["source_errors"]["subscriptions"] = type(exc).__name__

    try:
        waste = detect_waste_hotspots(db)
        result["waste_hotspots"] = len(waste)
    except Exception as exc:  # noqa: BLE001 - analytics source boundary
        logger.warning("analytics waste failed: %s", type(exc).__name__)
        result["waste_hotspots"] = None
        result["source_errors"]["waste_hotspots"] = type(exc).__name__

    if result["source_errors"]:
        result["status"] = "unavailable" if len(result["source_errors"]) == 2 else "insufficient_data"

    logger.info("job_daily_metrics: %s", result)
    return result


def job_daily_report(
    db_path: str,
    notifier: TelegramNotifier | None = None,
    deliver: bool = True,
) -> dict[str, Any]:
    """Compose and send morning report."""
    from jarvis.finance.scheduler.reports import (
        build_daily_report_data,
        render_daily_report,
    )
    from jarvis.finance.scheduler.telegram_notify import TelegramNotifier, send_once

    try:
        data = build_daily_report_data(db_path)
        text = render_daily_report(data)
        if not deliver:
            return {"status": data["status"], "sent": False, "database_modified": False, "report": data}
        sent = send_once(FinanceDB(db_path), notifier or TelegramNotifier(),
                         f"daily_report:{datetime.now(UTC).date().isoformat()}", text)
        return {"status": data["status"], "sent": sent, "report_database_modified": False,
                "delivery_attempted": True}
    except Exception as e:
        logger.exception("job_daily_report failed")
        return {"status": "error", "message": str(e)[:200]}


def job_anomaly_check(
    db_path: str,
    notifier: TelegramNotifier | None = None,
) -> dict[str, Any]:
    """Check for anomalies and alert if any."""
    from jarvis.finance.scheduler.telegram_notify import TelegramNotifier
    from jarvis.finance.supervisor.anomaly import run_anomaly_checks
    from jarvis.finance.supervisor.models import Severity

    notif = notifier or TelegramNotifier()
    try:
        findings = run_anomaly_checks(db_path)
        anomalies = [f for f in findings if f.severity in (Severity.WARNING, Severity.CRITICAL)]
        if anomalies:
            text = "⚠️ *Аномалии в тратах:*\n\n"
            for a in anomalies[:5]:
                text += f"• *{a.title}*: {a.detail}\n"
            from jarvis.finance.scheduler.telegram_notify import send_once
            send_once(FinanceDB(db_path), notif, f"anomaly:{datetime.now(UTC).strftime('%Y-%m-%dT%H')}", text)
        return {"status": "ok", "found": len(anomalies)}
    except Exception as e:
        logger.exception("job_anomaly_check failed")
        return {"status": "error", "message": str(e)[:200]}


def job_forecast_update(db_path: str) -> dict[str, Any]:
    """Daily forecast recalculation and metrics persistence."""
    try:
        report = _audited_forecast(db_path)
        return {"status": report["status"], "forecast": report}
    except Exception as e:
        logger.exception("job_forecast_update failed")
        return {"status": "error", "message": str(e)[:200]}


def job_sber_keepalive() -> dict[str, Any]:
    """Check and refresh Sber MCP session."""
    from jarvis.mcp.factory import get_sber_mcp

    client = get_sber_mcp()
    if not client:
        return {"status": "error", "message": "sber mcp client not available"}

    try:
        r = client.call_tool("sber_auth_start", {"mode": "auto"})
        status = r.get("status")
        state = r.get("state", "unknown")
        session_id = r.get("session_id", "?")
        if status != "ok" or state != "authenticated":
            return {"status": "error", "code": r.get("code"), "message": r.get("message")}
        return {"status": "ok", "session_id": session_id, "state": state}
    except Exception as e:
        logger.exception("job_sber_keepalive failed")
        return {"status": "error", "message": str(e)[:200]}
    finally:
        try:
            if hasattr(client, "close"):
                client.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001, S110
            pass


def job_market_scan_morning(
    db_path: str = "data/jarvis.db",
    top_n: int = 5,
    chat_id: int | str | None = None,
    notifier: TelegramNotifier | None = None,
    deliver: bool = True,
) -> str:
    """Morning market scan report to Telegram."""
    from jarvis.finance.scheduler.telegram_notify import TelegramNotifier, send_once
    from jarvis.services.invest_service import InvestService

    service = InvestService()
    text = service.get_market_scan(top_n=top_n)

    notif = notifier or TelegramNotifier(chat_id=chat_id)
    if deliver:
        send_once(FinanceDB(db_path), notif, f"market_scan:{datetime.now(UTC).date().isoformat()}", text)

    return text


def job_healthcheck(
    db_path: str = "data/jarvis.db",
    notifier: TelegramNotifier | None = None,
) -> dict[str, Any]:
    """Saved readiness only; notifier retained for compatibility, never called."""
    from jarvis.finance.readiness import local_readiness
    report = local_readiness(Path(db_path))
    return {**report, "details": report["sources"]}
