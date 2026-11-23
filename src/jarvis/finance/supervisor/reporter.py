"""Assemble full supervisor report."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from jarvis.finance.readiness import (
    local_readiness,
    readonly_snapshot,
    render_readiness,
)
from jarvis.finance.supervisor.anomaly import run_anomaly_checks
from jarvis.finance.supervisor.checks import run_all_checks
from jarvis.finance.supervisor.models import Finding, HealthReport, Severity


def _determine_overall(findings: list[Finding]) -> Severity:
    order = [Severity.CRITICAL, Severity.WARNING, Severity.INFO, Severity.OK]
    for sev in order:
        if any(f.severity == sev for f in findings):
            return sev
    return Severity.OK


def build_report(db_path: str, log_dir: str = "logs") -> HealthReport:
    readiness = local_readiness(Path(db_path))
    findings = [Finding("financial_readiness", Severity.WARNING,
                        "Financial data not production-accepted", readiness["status"])]
    if readiness["sources"]["sqlite"]["status"] == "available":
        try:
            with readonly_snapshot(Path(db_path)) as (snapshot, _):
                findings += run_all_checks(str(snapshot), log_dir)
                findings += run_anomaly_checks(str(snapshot))
        except (OSError, ValueError, sqlite3.Error) as exc:
            findings.append(Finding("snapshot_checks", Severity.CRITICAL, "Checks unavailable", type(exc).__name__))
    return HealthReport(
        generated_at=datetime.now(UTC).isoformat(),
        total_checks=len(findings),
        findings=findings,
        overall_severity=_determine_overall(findings),
        financial_readiness=readiness,
    )


EMOJI = {
    Severity.OK: "✅",
    Severity.INFO: "ℹ️",
    Severity.WARNING: "⚠️",
    Severity.CRITICAL: "🔴",
}


def render_health_report(report: HealthReport, include_ok: bool = False) -> str:
    lines = ["🩺 System Health Report"]
    lines.append(f"Общее состояние: {EMOJI[report.overall_severity]} {report.overall_severity.value}")
    lines.append(f"Проверок: {report.total_checks}")
    lines.append("")
    if report.financial_readiness is not None:
        lines.append(render_readiness(report.financial_readiness))

    for f in report.findings:
        if not include_ok and f.severity == Severity.OK:
            continue
        emoji = EMOJI[f.severity]
        lines.append(f"{emoji} {f.title}")
        if f.detail:
            lines.append(f"   {f.detail}")
        if f.suggestion:
            lines.append(f"   💡 {f.suggestion}")

    if report.llm_summary:
        lines.append("")
        lines.append("🧠 LLM-анализ:")
        lines.append(report.llm_summary)

    return "\n".join(lines)
