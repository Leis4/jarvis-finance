"""Notification data models."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class EventType(str, Enum):
    ANOMALY = "anomaly"
    FORECAST_NEGATIVE = "forecast_negative"
    NEW_SUBSCRIPTION = "new_subscription"
    LOW_BALANCE = "low_balance"
    NEW_COMPENSATION = "new_compensation"
    LARGE_EXPENSE = "large_expense"


@dataclass
class NotificationEvent:
    event_type: EventType
    severity: Severity
    title: str
    message: str
    data: dict[str, str | int | float | None] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    dedup_key: str = ""
