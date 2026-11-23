"""Render scanner results as Telegram Markdown."""
from __future__ import annotations

from jarvis.invest.scanner.stock_scanner import StockCandidate


def render_stock_scan(
    candidates: list[StockCandidate],
    days: int = 365,
) -> str:
    """Render top-N stock candidates."""
    if not candidates:
        return "📭 Сканер: нет кандидатов по текущим фильтрам."

    lines = [f"📈 *Топ-{len(candidates)} акций MOEX* (за {days} дн.)", "research_only; price-only; Sharpe: zero reference, fees/FX/corporate actions unverified."]
    for i, c in enumerate(candidates, 1):
        lines.append(f"{i}. *{c.ticker}* — {c.last_price:.2f} ₽")
        lines.append(
            f"   Доходность: {c.total_return:+.1%} | "
            f"Волатильность: {c.volatility:.1%}"
        )
        lines.append(
            f"   Sharpe: {c.sharpe:+.2f} | Max DD: {c.max_drawdown:.1%}"
        )
    return "\n".join(lines)
