"""Pure Telegram inline-keyboard builder for Jarvis' personal finance menu.

The module performs no I/O, no financial calculations and has no side
effects.  It only validates the optional pending-review count and returns a
fresh, deeply independent keyboard structure on every call.
"""

from __future__ import annotations

# (callback_data, label, shows_pending_count)
_BUTTONS: tuple[tuple[str, str, bool], ...] = (
    ("m:menu", "\U0001F3E0 Главная", False),
    ("m:balance", "\U0001F4B3 Счета и баланс", False),
    ("m:spend", "\U0001F4CA Траты месяца", False),
    ("m:analysis", "\U0001F4C8 Аналитика", False),
    ("m:goals", "\U0001F3AF Цели", False),
    ("m:ai", "\U0001F916 AI", False),
    ("m:review", "\U0001F9E0 Обучение", True),
    ("m:refresh", "\U0001F504 Сбер", False),
    ("m:settings", "\u2699\uFE0F Настройки", False),
)

_BUTTONS_PER_ROW = 2


def _validate_pending_count(pending_count: int | None) -> int | None:
    """Return a validated count, or ``None`` when the count is unknown.

    ``None`` explicitly means "unknown"; it is never treated as zero.
    Booleans, negative numbers and non-integers are rejected.
    """
    if pending_count is None:
        return None
    if isinstance(pending_count, bool) or not isinstance(pending_count, int):
        raise TypeError(
            "pending_count must be None or a non-negative int, "
            f"got {type(pending_count).__name__}"
        )
    if pending_count < 0:
        raise ValueError(f"pending_count must be non-negative, got {pending_count}")
    return pending_count


def main_menu(
    pending_count: int | None = None,
) -> dict[str, list[list[dict[str, str]]]]:
    """Build the nine-button Telegram inline keyboard for the main menu.

    Args:
        pending_count: Number of pending training/review items.  ``None``
            means the count is unknown, so the review button stays neutral.

    Returns:
        ``{"inline_keyboard": [[button, ...], ...]}`` with at most two
        buttons per row.

    Raises:
        TypeError: If the count is not an integer.
        ValueError: If the count is negative.
    """
    count = _validate_pending_count(pending_count)

    buttons: list[dict[str, str]] = []
    for callback_data, label, shows_count in _BUTTONS:
        if shows_count and count is not None:
            label = f"{label} ({count})"
        buttons.append({"text": label, "callback_data": callback_data})

    rows = [
        buttons[index : index + _BUTTONS_PER_ROW]
        for index in range(0, len(buttons), _BUTTONS_PER_ROW)
    ]
    return {"inline_keyboard": rows}
