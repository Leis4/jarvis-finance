"""Data models for supervisor."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    OK = "OK"
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class Finding:
    check_name: str
    severity: Severity
    title: str
    detail: str
    affected: list[str] = field(default_factory=list)  # tx_ids, dates, etc
    suggestion: str | None = None


@dataclass
class HealthReport:
    generated_at: str
    total_checks: int
    findings: list[Finding]
    llm_summary: str | None = None
    overall_severity: Severity = Severity.OK
    financial_readiness: dict[str, Any] | None = None

    @property
    def is_healthy(self) -> bool:
        return (self.financial_readiness is not None and self.financial_readiness["status"] == "available"
                and all(f.severity in (Severity.OK, Severity.INFO) for f in self.findings))
