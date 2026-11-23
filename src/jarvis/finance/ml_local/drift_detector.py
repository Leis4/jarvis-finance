"""Detect model drift from prediction log."""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jarvis.finance.db import FinanceDB


@dataclass
class DriftReport:
    window_days: int
    total_predictions: int
    avg_confidence: float
    qwen_fallback_pct: float
    manual_fix_pct: float
    drift_detected: bool
    reasons: list[str]


def detect_drift(db: FinanceDB, window_days: int = 7) -> DriftReport:
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT confidence, used_qwen, user_corrected FROM prediction_log WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()

    total = len(rows)
    reasons = []

    if total < 100:
        return DriftReport(
            window_days=window_days,
            total_predictions=total,
            avg_confidence=0.0,
            qwen_fallback_pct=0.0,
            manual_fix_pct=0.0,
            drift_detected=False,
            reasons=["insufficient_data"],
        )

    avg_conf = sum(float(r["confidence"]) for r in rows) / total
    qwen_pct = sum(int(r["used_qwen"]) for r in rows) / total
    fix_pct = sum(int(r["user_corrected"]) for r in rows) / total

    if avg_conf < 0.6:
        reasons.append(f"avg_confidence={avg_conf:.2f} < 0.6")
    if fix_pct > 0.15:
        reasons.append(f"manual_fix_pct={fix_pct:.1%} > 15%")
    if qwen_pct > 0.3:
        reasons.append(f"qwen_fallback_pct={qwen_pct:.1%} > 30%")

    return DriftReport(
        window_days=window_days,
        total_predictions=total,
        avg_confidence=avg_conf,
        qwen_fallback_pct=qwen_pct,
        manual_fix_pct=fix_pct,
        drift_detected=len(reasons) > 0,
        reasons=reasons,
    )


def render_drift_report(report: DriftReport) -> str:
    if "insufficient_data" in report.reasons:
        return (
            f"📊 Drift Status ({report.window_days} days)\n"
            f"Predictions: {report.total_predictions}\n"
            f"Avg confidence: 0.00 ✓\n"
            f"Qwen fallback: 0.0% ✓\n"
            f"Manual fixes: 0.0% ✓\n"
            f"Status: ℹ️ Insufficient Data (<100 needed)"
        )

    status_icon = "⚠️ DRIFT DETECTED" if report.drift_detected else "✅ OK"
    conf_icon = "✓" if report.avg_confidence >= 0.6 else "❌"
    qwen_icon = "✓" if report.qwen_fallback_pct <= 0.3 else "❌"
    fix_icon = "✓" if report.manual_fix_pct <= 0.15 else "❌"

    return (
        f"📊 Drift Status ({report.window_days} days)\n"
        f"Predictions: {report.total_predictions}\n"
        f"Avg confidence: {report.avg_confidence:.2f} {conf_icon}\n"
        f"Qwen fallback: {report.qwen_fallback_pct:.1%} {qwen_icon}\n"
        f"Manual fixes: {report.manual_fix_pct:.1%} {fix_icon}\n"
        f"Status: {status_icon}"
    )
