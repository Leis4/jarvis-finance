"""Local, read-only presentation of the daily personal financial briefing.

This module only formats already prepared data. It performs no financial
calculations, does not mutate its input, and never invents missing facts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["render_daily_briefing"]

MAX_CHARS = 3900
MAX_TITLE_CHARS = 160
MAX_REASON_CHARS = 420
MAX_ACTION_CHARS = 100
MAX_PERIOD_CHARS = 80
MIN_LIMIT = 1
MAX_LIMIT = 5

_EVENT_PREFIXES = {
    "FACT": "",
    "INFERENCE": "Гипотеза: ",
    "FORECAST": "Прогноз: ",
    "OWNER_DECISION": "Решение владельца: ",
    "STALE": "Устаревшие данные: ",
    "UNKNOWN": "Неизвестно: ",
}

_MISSING_PRIORITY = 10**9
_EMPTY_STATE = "Нет событий, подтверждённых докниколайельствами."
_TRUNCATION_NOTE = "Сокращено из-за ограничения объёма."


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer from 1 to 5")
    if not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise ValueError("limit must be an integer from 1 to 5")


def _clean_text(value: Any, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _money(value: Any) -> str:
    """Preserve a provided money string byte-for-byte; never derive amounts."""
    if not isinstance(value, str):
        return "неизвестно"
    if not value.strip():
        return "неизвестно"
    if value.rstrip().endswith("₽"):
        return value
    return f"{value} ₽"


def _valid_event(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None

    event_id = raw.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        return None

    title = _clean_text(raw.get("title"), MAX_TITLE_CHARS)
    reason = _clean_text(raw.get("reason"), MAX_REASON_CHARS)
    if not title or not reason:
        return None

    evidence = raw.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not any(isinstance(item, Mapping) for item in evidence)
    ):
        return None

    epistemic = raw.get("epistemic")
    if not isinstance(epistemic, str):
        return None
    epistemic = epistemic.strip()
    if epistemic not in _EVENT_PREFIXES:
        return None

    period = raw.get("period")
    if not isinstance(period, Mapping):
        return None
    period_start = _clean_text(period.get("start"), MAX_PERIOD_CHARS)
    period_end = _clean_text(period.get("end"), MAX_PERIOD_CHARS)
    if not period_start or not period_end:
        return None

    action = raw.get("action")
    if not isinstance(action, Mapping):
        return None
    action_label = _clean_text(action.get("label"), MAX_ACTION_CHARS)
    if not action_label:
        return None

    priority = raw.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int):
        priority = _MISSING_PRIORITY

    return {
        "id": event_id.strip(),
        "epistemic": epistemic,
        "title": title,
        "reason": reason,
        "period_start": period_start,
        "period_end": period_end,
        "action": action_label,
        "priority": priority,
    }


def _select_events(brief: Mapping[str, Any], limit: int) -> list[dict[str, Any]]:
    raw_events = brief.get("events")
    if not isinstance(raw_events, list):
        return []

    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_events:
        event = _valid_event(raw)
        if event is None:
            continue
        if event["id"] in seen_ids:
            continue
        seen_ids.add(event["id"])
        selected.append(event)

    # sorted() is stable, so equal priorities keep their original order.
    selected.sort(key=lambda event: event["priority"])
    return selected[:limit]


def _event_block(index: int, event: Mapping[str, Any]) -> str:
    prefix = _EVENT_PREFIXES[event["epistemic"]]
    lines = [
        f"{index}. {prefix}{event['title']}",
        f"Причина: {event['reason']}",
        f"Период: {event['period_start']} — {event['period_end']}",
        f"Действие: {event['action']}",
    ]
    return "\n".join(lines)


def _render_header(brief: Mapping[str, Any]) -> str:
    position = brief.get("position")
    if not isinstance(position, Mapping):
        position = {}

    observed_at = _clean_text(brief.get("observed_at"), MAX_PERIOD_CHARS)
    if not observed_at:
        observed_at = "неизвестно"

    header = "\n".join(
        [
            "Что важно сегодня",
            "",
            f"Банк: {_money(position.get('bank_total'))}",
            f"Доступно: {_money(position.get('spendable'))}",
            f"Данные на: {observed_at}",
        ]
    )
    flow = brief.get('cashflow')
    if isinstance(flow, Mapping):
        bounds = flow.get('period')
        if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
            header += f"\nПериод: {_clean_text(bounds[0], 80)} — {_clean_text(bounds[1], 80)}"
        header += (f"\nИзвестные расходы: {_money(flow.get('expenses'))}"
            f"\nИзвестные поступления: {_money(flow.get('income'))}"
            '\nПолнота истории не доказана; переводы исключены.')
    return header


def render_daily_briefing(brief: dict[str, Any], *, limit: int = 3) -> str:
    """Render a ready-to-read Russian plain-text daily financial briefing.

    Only events with an id, title, reason, evidence, valid epistemic status,
    complete period and an action label are shown. At most ``limit`` events
    are displayed in ascending priority order, preserving the original order
    for ties. Money strings are copied as-is; missing values are reported as
    unknown rather than as zero.
    """
    _validate_limit(limit)
    if not isinstance(brief, Mapping):
        raise TypeError("brief must be a mapping")

    header = _render_header(brief)
    events = _select_events(brief, limit)
    event_blocks = [_event_block(index, event) for index, event in enumerate(events, 1)]

    if not event_blocks:
        text = f"{header}\n\n{_EMPTY_STATE}"
    else:
        text = "\n\n".join([header, *event_blocks])

    omitted = False
    while len(text) > MAX_CHARS and event_blocks:
        event_blocks.pop()
        omitted = True
        if event_blocks:
            text = "\n\n".join([header, *event_blocks])
        else:
            text = f"{header}\n\n{_EMPTY_STATE}"

    if omitted:
        candidate = f"{text}\n\n{_TRUNCATION_NOTE}"
        if len(candidate) <= MAX_CHARS:
            text = candidate

    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 1].rstrip() + "…"

    return text
