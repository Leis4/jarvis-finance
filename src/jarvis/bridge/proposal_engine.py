from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvis.bridge.free_cash import get_free_cash
from jarvis.finance.db import FinanceDB


@dataclass
class TradeProposal:
    symbol: str
    side: str  # "buy" | "sell"
    quantity: int
    price: float
    reasoning: str
    risk_score: float  # 0..1
    source: str  # "llm" | "rule"


def build_proposal(
    db_finance: FinanceDB,
    portfolio: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    max_position_pct: float = 0.10,
) -> TradeProposal | None:
    """Deterministic proposal generation without LLM."""
    free = get_free_cash(db_finance)
    if free < 1000.0:
        return None
    if not candidates:
        return None

    top = candidates[0]
    symbol = str(top.get("symbol") or top.get("ticker") or top.get("instrument_id") or "")
    if not symbol:
        return None

    price = float(top.get("price", 0.0))
    if price <= 0.0:
        return None

    portfolio_value = sum(float(p.get("value", 0.0) or p.get("market_value", 0.0)) for p in portfolio) or free
    max_position_size = min(free, portfolio_value * max_position_pct)

    qty = int(max_position_size / price)
    if qty < 1:
        return None

    reasoning = str(top.get("reason") or top.get("reasoning") or "top-ranked candidate")
    risk_score = float(top.get("score") or top.get("risk_score") or 0.5)

    return TradeProposal(
        symbol=symbol,
        side="buy",
        quantity=qty,
        price=price,
        reasoning=reasoning,
        risk_score=risk_score,
        source="rule",
    )


def render_proposal(tp: TradeProposal) -> str:
    """Markdown rendering of proposal for Telegram."""
    total_cost = tp.quantity * tp.price
    return (
        f"**Предложение по инвестированию**\n\n"
        f"• **Инструмент:** `{tp.symbol}`\n"
        f"• **Действие:** {tp.side.upper()}\n"
        f"• **Количество:** {tp.quantity}\n"
        f"• **Цена:** {tp.price:.2f} RUB\n"
        f"• **Сумма:** {total_cost:.2f} RUB\n"
        f"• **Обоснование:** {tp.reasoning}\n"
        f"• **Риск-оценка:** {tp.risk_score:.2f}\n"
        f"• **Источник:** {tp.source}\n"
    )
