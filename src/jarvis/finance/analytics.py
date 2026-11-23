from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from jarvis.finance.db import FinanceDB


@dataclass(frozen=True)
class SubscriptionCandidate:
    merchant: str
    category: str
    average_amount: Decimal
    occurrences: int
    first_date: str
    last_date: str
    confidence: str
    recommendation: str


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def detect_recurring_subscriptions(db: FinanceDB, min_occurrences: int = 2) -> list[SubscriptionCandidate]:
    txs = [tx for tx in db.all_transactions() if money(tx["amount"]) < 0]
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for tx in txs:
        merchant = (str(tx.get("merchant") or tx.get("description") or "unknown").strip() or "unknown").lower()
        category = str(tx.get("category") or "uncategorized")
        amount_bucket = str(abs(money(tx["amount"])).quantize(Decimal(1)))
        grouped.setdefault((merchant, category, amount_bucket), []).append(tx)

    candidates: list[SubscriptionCandidate] = []
    for (merchant, category, _amount_bucket), rows in grouped.items():
        if len(rows) < min_occurrences:
            continue
        dates = sorted(date.fromisoformat(str(row["tx_date"])) for row in rows)
        if len(dates) >= 2:
            gaps = [(b - a).days for a, b in itertools.pairwise(dates)]
            avg_gap = sum(gaps) / len(gaps) if gaps else 0
        else:
            avg_gap = 0
        if category != "subscriptions" and not (25 <= avg_gap <= 35):
            continue
        amounts = [abs(money(row["amount"])) for row in rows]
        avg_amount = (sum(amounts, Decimal("0.00")) / Decimal(str(len(amounts)))).quantize(Decimal("0.01"))
        confidence = "high" if category == "subscriptions" or 25 <= avg_gap <= 35 else "medium"
        recommendation = "Проверь, пользуешься ли этим. Если нет — создай действие на отмену подписки вручную."
        candidates.append(
            SubscriptionCandidate(
                merchant=merchant,
                category=category,
                average_amount=avg_amount,
                occurrences=len(rows),
                first_date=dates[0].isoformat(),
                last_date=dates[-1].isoformat(),
                confidence=confidence,
                recommendation=recommendation,
            )
        )
    return sorted(candidates, key=lambda c: (-c.occurrences, c.merchant))


def detect_waste_hotspots(db: FinanceDB, days: int = 30) -> list[dict[str, Any]]:
    cutoff = date.today() - timedelta(days=days)  # noqa: DTZ011
    totals: dict[str, Decimal] = {}
    for tx in db.all_transactions():
        tx_date = date.fromisoformat(str(tx["tx_date"]))
        amount = money(tx["amount"])
        if tx_date < cutoff or amount >= 0:
            continue
        category = str(tx.get("category") or "uncategorized")
        if category not in {"delivery", "entertainment", "shopping", "subscriptions", "uncategorized"}:
            continue
        totals[category] = totals.get(category, Decimal("0.00")) + abs(amount)
    rows = []
    for category, total in sorted(totals.items(), key=lambda item: item[1], reverse=True):
        suggested_cut = (total * Decimal("0.20")).quantize(Decimal("0.01"))
        rows.append({
            "category": category,
            "period_days": days,
            "spent": total.quantize(Decimal("0.01")),
            "suggested_cut": suggested_cut,
            "message": f"Снизить {category} на 20% даст около {suggested_cut} RUB экономии за {days} дней.",
        })
    return rows


def render_subscriptions(candidates: list[SubscriptionCandidate]) -> str:
    if not candidates:
        return "Повторяющиеся подписки не найдены."
    lines = ["Возможные подписки:", ""]
    for c in candidates:
        lines.append(
            f"- {c.merchant}: ~{c.average_amount} RUB, {c.occurrences} раз, "
            f"{c.first_date}..{c.last_date}, confidence={c.confidence}. {c.recommendation}"
        )
    return "\n".join(lines)


def render_waste_hotspots(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Явных зон для быстрой экономии не найдено."
    lines = ["Зоны быстрой экономии:", ""]
    for row in rows:
        lines.append(f"- {row['category']}: потрачено {row['spent']} RUB. {row['message']}")
    return "\n".join(lines)
