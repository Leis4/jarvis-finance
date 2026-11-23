"""Explicit scanner source and input failures."""

import math
from datetime import UTC, date, datetime
from typing import Any


class ScannerUnavailable(RuntimeError):
    """Source failed; no successful empty recommendation is available."""


class ScannerInsufficientData(ValueError):
    """Inputs do not support a scanner metric or recommendation."""


def number(value: Any, *, positive: bool = False) -> float:
    try:
        if value is None or isinstance(value, bool):
            raise ValueError
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ScannerInsufficientData("Missing or invalid numeric input") from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise ScannerInsufficientData("Nonfinite or nonpositive input")
    return result


def fresh_candles(candles: list[dict[str, Any]]) -> str:
    """Require ordered daily observations no older than seven calendar days."""
    try:
        dates = [date.fromisoformat(str(c.get("begin") or c.get("TRADEDATE") or "")[:10]) for c in candles]
    except ValueError:
        raise ScannerInsufficientData("Observation dates unavailable") from None
    today = datetime.now(UTC).date()
    if not dates or dates != sorted(set(dates)) or dates[-1] > today or (today - dates[-1]).days > 7:
        raise ScannerInsufficientData("Unordered, future or stale observations")
    return dates[-1].isoformat()
