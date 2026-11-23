from __future__ import annotations

from decimal import Decimal
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.rules import decimal_money


def plan_month_end_reserve_sweep(db: FinanceDB, rules: dict[str, Any]) -> tuple[int | None, str]:
    snapshot = db.latest_budget_snapshot()
    if snapshot is None:
        return None, "Нет budget snapshot. Сначала внеси остатки через add-snapshot или web-интерфейс."

    reserve_balance = decimal_money(snapshot["reserve_balance"])
    reserve_minimum = decimal_money(rules.get("reserve_minimum", 50000))
    surplus = (reserve_balance - reserve_minimum).quantize(Decimal("0.01"))
    if surplus <= 0:
        return None, (
            f"Перевод не нужен: резерв {reserve_balance} RUB не выше минимальной цели {reserve_minimum} RUB. "
            "Система не будет ослаблять резерв ради накоплений."
        )

    action_id = db.create_action_request(
        "reserve_sweep_to_savings",
        {
            "from": "reserve",
            "to": "savings",
            "amount": str(surplus),
            "reserve_balance_before": str(reserve_balance),
            "reserve_minimum": str(reserve_minimum),
            "snapshot_id": snapshot["id"],
        },
        reason="Month-end surplus above reserve minimum can be moved to savings after confirmation.",
        approval_required=True,
    )
    return action_id, (
        f"Создано действие #{action_id}: перевести {surplus} RUB из резерва в накопления. "
        f"После перевода резерв останется на уровне {reserve_minimum} RUB."
    )
