"""Coverage-gated adapter to the audited chronological research baseline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from jarvis.finance.audit_report import build_audit_report
from jarvis.finance.db import FinanceDB
from jarvis.finance.source_health import source_health


@dataclass
class ForecastReport:
    """Keep unavailable metrics null, including deprecated unmeasured statistics."""
    generated_at: str
    horizon_days: int
    status: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    projected_balance_in_14d: float | None = None
    used_model: str | None = None
    daily_avg: float | None = None
    weekly_forecast: float | None = None
    weekly_lower: float | None = None
    weekly_upper: float | None = None
    days_to_zero: int | None = None
    safe_daily: float | None = None
    kalman_trend: str | None = None
    monte_carlo_p10: float | None = None
    monte_carlo_p50: float | None = None
    monte_carlo_p90: float | None = None
    prob_solvent: float | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return reproducible evidence without fabricated compatibility metrics."""
        return asdict(self)


def compute_forecast(db: FinanceDB, horizon_days: int = 14, *, now: datetime | None = None) -> ForecastReport:
    """Require declared inventory, reconciled coverage and fresh successful sources."""
    if not 1 <= horizon_days <= 365:
        raise ValueError("horizon_days must be positive and at most 365")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(UTC)
    end = current.date()
    audit = build_audit_report(db, end - timedelta(days=29), end, now=current, forecast_horizon=horizon_days)
    result = ForecastReport(current.isoformat(), horizon_days, "insufficient_data", "Inventory/coverage/balances are unverified", evidence=audit)
    if not db.get_setting("expected_accounts", []) or audit["status"] != "complete":
        return result
    required = sorted(set(audit["sources"]) | {a["source"] for a in audit["accounts"] if a["source"]})
    states = source_health(db, required, current)
    audit["forecast_source_health"] = states
    if any(s["availability_status"] == "stale" for s in states.values()):
        result.status, result.reason = "stale", "Required source evidence expired or clock inconsistent"
        return result
    if any(s["availability_status"] in {"unavailable", "blocked"}
           and s["availability"]["source_error"] != "No saved source evidence" for s in states.values()):
        result.status, result.reason = "unavailable", "Required source evidence unavailable"
        return result
    if any(s["availability_status"] == "insufficient_data" for s in states.values()):
        result.reason = "Source observation time missing"
        return result
    if any(s["status"] != "ok" and (s.get("checked_at") or s.get("last_success_at")) for s in states.values()):
        result.status, result.reason = "unavailable", "Required source attempt failed"
        return result
    if not required or any(s.get("age_seconds") is None for s in states.values()):
        result.reason = "Source freshness is unknown"
        return result
    if any(s["age_seconds"] > 86400 or (s.get("coverage_end") or "") < end.isoformat() for s in states.values()):
        result.status, result.reason = "stale", "Required source coverage/success is stale (24h policy)"
        return result
    forecast = audit["forecast"]
    if forecast["status"] != "experimental":
        result.status = forecast["status"] if forecast["status"] in {"unavailable", "stale", "insufficient_data"} else "unavailable"
        result.reason = forecast.get("reason", "Insufficient chronological history")
        return result
    result.status = "experimental"
    result.reason = "Research baseline; obligations and economic semantics are not production-accepted"
    result.used_model = forecast["method"]
    # Historical attribute is retained for the notification caller; horizon is explicit.
    result.projected_balance_in_14d = float(forecast["projected_balance"])
    result.warnings = [forecast["assumption"], forecast["evaluation"]]
    return result


def render_forecast(report: ForecastReport, format_mode: str = "cli") -> str:
    """Explain refusals and research output without unmeasured probabilities."""
    lines = [f"Forecast: {report.status}", report.reason]
    if report.status == "experimental":
        forecast = report.evidence["forecast"]
        lines.extend([f"Research baseline: {report.used_model}",
                      f"Projected balance in {report.horizon_days} days: {forecast['projected_balance']} RUB",
                      f"Bootstrap p10/p90: {forecast['p10']} / {forecast['p90']} RUB",
                      f"Period: {report.evidence['filters']['start']} .. {report.evidence['filters']['end']}"])
        lines.extend(report.warnings)
    return "\n".join(lines)
