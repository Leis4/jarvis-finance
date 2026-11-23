from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from jarvis.bridge.free_cash import FreeCashUnavailable
from jarvis.finance.db import FinanceDB
from jarvis.services.invest_service import InvestService


def _fmt_money(amount: Any, currency: str = "RUB") -> str:
    try:
        if isinstance(amount, bool) or len(str(amount)) > 64:
            return "недоступно"
        value = Decimal(str(amount))
        if not value.is_finite() or value.copy_abs() > Decimal("1e18"):
            return "недоступно"
        return f"{value:,.2f}".replace(",", " ") + (" ₽" if currency == "RUB" else f" {currency}")
    except (InvalidOperation, TypeError, ValueError):
        return "недоступно"


def render_balances(data: dict[str, Any]) -> str:
    """Render balance command response."""
    if "error" in data:
        return f"❌ {data['error']}"
    lines = ["💰 *Балансы*", ""]
    total = data.get("total_rub")
    lines.append(f"*Наблюдаемые RUB-счета: {_fmt_money(total)}*")
    lines.append("Полнота банковского покрытия не подтверждена; карты не добавляются к сумме счетов.")
    lines.append("")
    accounts = data.get("accounts", [])
    if accounts:
        lines.append("*Счета:*")
        for acc in accounts:
            name = acc.get("name", "?")
            last4 = acc.get("last4", "?")
            bal = acc.get("balance", {})
            amount = bal.get("amount") if isinstance(bal, dict) else None
            currency = bal.get("currency") if isinstance(bal, dict) else None
            rendered = _fmt_money(amount, currency) if isinstance(currency, str) and len(currency) == 3 and currency.isascii() and currency.isalpha() and currency.isupper() else "недоступно: валюта неизвестна"
            lines.append(f"  • {name} ••{last4}: {rendered}")
    cards = data.get("cards", [])
    if cards:
        lines.append("")
        lines.append("*Карты:*")
        for card in cards:
            name = card.get("name", "?")
            last4 = card.get("last4", "?")
            bal = card.get("balance", {})
            amount = bal.get("amount") if isinstance(bal, dict) else None
            currency = bal.get("currency") if isinstance(bal, dict) else None
            rendered = _fmt_money(amount, currency) if isinstance(currency, str) and len(currency) == 3 and currency.isascii() and currency.isalpha() and currency.isupper() else "недоступно: валюта неизвестна"
            lines.append(f"  • {name} ••{last4}: {rendered}")
    return "\n".join(lines)


def render_operations(ops: list[dict[str, Any]], days: int = 7) -> str:
    """Render operations command response."""
    if not ops:
        return f"📭 Операций за {days} дней нет."
    lines = [f"📋 *Последние операции ({days} дн.)*", ""]
    total_out = 0.0
    total_in = 0.0
    for op in ops[:20]:
        date = str(op.get("date", ""))[:10]
        merchant = str(op.get("merchant") or op.get("description") or "?")[:35]
        amt = op.get("amount", {})
        amount_str = str(amt.get("amount", "0")) if isinstance(amt, dict) else "0"
        try:
            amount = float(amount_str)
        except (TypeError, ValueError):
            amount = 0.0
        if amount < 0:
            total_out += abs(amount)
            sign = ""
        else:
            total_in += amount
            sign = "+"
        emoji = "🔴" if amount < 0 else "🟢"
        lines.append(f"{emoji} {date} | {merchant}")
        lines.append(f"     {sign}{_fmt_money(abs(amount))}")
    lines.append("")
    lines.append(f"➖ Расходы: {_fmt_money(total_out)}")
    lines.append(f"➕ Приходы: {_fmt_money(total_in)}")
    return "\n".join(lines)


def render_ticker(data: dict[str, Any]) -> str:
    """Render market command response."""
    if "error" in data:
        return f"❌ {data['error']}"
    symbol = data.get("ticker", "?")
    last = data.get("last")
    if last is None:
        return f"📊 *{symbol}*: нет данных (торги закрыты)"
    lines = [f"📊 *{symbol}*: {_fmt_money(last)}"]
    change = data.get("change_pct")
    if change is not None:
        try:
            ch = float(change)
            arrow = "🔺" if ch > 0 else ("🔻" if ch < 0 else "➖")
            lines.append(f"{arrow} {ch:+.2f}%")
        except (TypeError, ValueError):
            pass
    lines.append("")
    history = data.get("history", [])
    if history:
        lines.append("*Последние дни:*")
        for row in history:
            date = row.get("TRADEDATE", "?")
            close = row.get("CLOSE")
            if close is not None:
                lines.append(f"  • {date}: {_fmt_money(close)}")
    return "\n".join(lines)


def render_free_cash(amount: float) -> str:
    """Render free cash response."""
    if amount <= 0:
        return (
            "💸 *Свободных денег нет*\n\n"
            "Все средства распределены на текущие расходы и резерв."
        )
    if amount < 1000:
        return (
            f"💸 Свободно: {_fmt_money(amount)}\n"
            "Маловато для инвестиций — накапливай дальше."
        )
    return (
        f"✅ *Свободно для инвестиций: {_fmt_money(amount)}*\n\n"
        "Можно рассмотреть покупку ОФЗ или индексных фондов."
    )


def get_default_dates(days: int = 7) -> tuple[str, str]:
    """Return (from_date, to_date) in YYYY-MM-DD."""
    today = datetime.now(UTC).date()
    frm = today - timedelta(days=days)
    return frm.isoformat(), today.isoformat()


# --- Handlers ---

class InvestCommands:
    """Container for invest-related command handlers."""

    def __init__(self, service: InvestService, db: FinanceDB) -> None:
        self.service = service
        self.db = db

    def handle_balance(self) -> str:
        try:
            data = self.service.get_balances()
            return render_balances(data)
        except Exception as e:  # noqa: BLE001
            return f"❌ Ошибка: {type(e).__name__}: {str(e)[:200]}"

    def handle_operations(self, days: int = 7) -> str:
        try:
            frm, till = get_default_dates(days)
            ops = self.service.get_operations(frm, till, limit=20)
            return render_operations(ops, days=days)
        except Exception as e:  # noqa: BLE001
            return f"❌ Ошибка: {type(e).__name__}: {str(e)[:200]}"

    def handle_market(self, symbol: str) -> str:
        try:
            data = self.service.get_ticker(symbol.upper())
            return render_ticker(data)
        except Exception as e:  # noqa: BLE001
            return f"❌ Ошибка: {type(e).__name__}: {str(e)[:200]}"

    def handle_free(self) -> str:
        try:
            amount = self.service.get_free_cash(self.db)
            return render_free_cash(amount)
        except FreeCashUnavailable:
            return (
                "Свободные средства: insufficient_data. Нужны подтверждённые "
                "остатки всех счетов, обязательства на выбранный период и резерв. "
                "Сумма не рассчитана; БД не изменена."
            )
        except Exception as e:  # noqa: BLE001
            return f"❌ Ошибка: {type(e).__name__}: {str(e)[:200]}"

    def handle_scan(self, top_n: int = 10) -> str:
        try:
            return self.service.get_market_scan(top_n=top_n)
        except Exception as e:  # noqa: BLE001
            return f"❌ Ошибка: {type(e).__name__}: {str(e)[:200]}"

    def handle_bonds(self, top_n: int = 10) -> str:
        try:
            return self.service.get_bond_scan(top_n=top_n)
        except Exception as e:  # noqa: BLE001
            return f"❌ Ошибка: {type(e).__name__}: {str(e)[:200]}"

    def handle_risk(self, tickers: list[str] | None = None) -> str:
        try:
            from jarvis.invest.risk.advanced import RiskInputError, portfolio_metrics

            t_list = tickers if tickers else ["SBER", "GAZP", "LKOH"]
            prices: dict[str, list[float]] = {}
            observation_dates: dict[str, list[str]] = {}
            if self.service._moex:
                for t in t_list:
                    candles = self.service._moex.get_candles(t, interval=24, days=60)
                    if candles:
                        prices[t] = [float(c["close"] if "close" in c else c["CLOSE"]) for c in candles]
                        observation_dates[t] = [str(c.get("begin") or c.get("TRADEDATE") or "")[:10]
                                                for c in candles]

            if not prices:
                return "❌ Риск недоступен: нет котировок MOEX."
            missing = set(t_list) - set(prices)
            if missing or any(len(values) < 3 or any(p <= 0 for p in values) for values in prices.values()):
                return "❌ Риск недоступен: неполные или некорректные котировки MOEX."

            try:
                metrics = portfolio_metrics(prices, observation_dates=observation_dates)
            except RiskInputError:
                return "❌ Риск недоступен: insufficient_data или некорректные/устаревшие котировки."
            mc = metrics["mc_30d"]
            return (
                "📊 *Исследование риска: равные веса, price-only*\n"
                f"Нижний квантиль доходности 95% (день): {metrics['var_95'] * 100:.1f}%\n"
                f"Средняя доходность в хвосте (CVaR): {metrics['cvar_95'] * 100:.1f}%\n"
                f"MC (30д): P5 {mc['p5'] * 100:.1f}%, P50 {mc['p50'] * 100:.1f}%, P95 {mc['p95'] * 100:.1f}%"
                "\nresearch_only; дивиденды, валютная переоценка и тарифы не подтверждены."
            )
        except Exception as e:  # noqa: BLE001
            return f"❌ Риск недоступен: unavailable ({type(e).__name__})."

    def handle_stress(self, scenario: str = "2008_crisis") -> str:
        return "❌ Стресс-тест недоступен: источник позиций портфеля не подключён."

    def handle_optimal(self) -> str:
        try:
            opt_path = Path("data/research/walk_forward.json")
            if not opt_path.exists():
                return "Инвестиционная оценка недоступна: проверенного walk-forward отчёта нет. См. docs/ML_INVEST_VALIDATION.md."
            with open(opt_path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("status") != "research_only" or not data.get("data_sha256") or not data.get("folds"):
                return "Инвестиционная оценка недоступна: нет provenance или результатов walk-forward."
            weights = data["folds"][-1]["weights"]
            w_str = ", ".join(f"{k}: {v*100:.1f}%" for k, v in weights.items())
            return (
                "Исследование minimum-variance; production BLOCKED\n"
                f"Последняя тестовая аллокация: {w_str}\n"
                f"Период данных: {' … '.join(data['period'])}\n"
                f"Исторический результат: {data['strategy']['total_return']:.2%}\n"
                f"Равновзвешенный benchmark: {data['equal_weight_benchmark']['total_return']:.2%}\n"
                f"Издержки: fee {data['fee_bps']} + slippage {data['slippage_bps']} bps\n"
                "Дивиденды, corporate actions, лоты и тарифы не проверены."
            )
        except Exception as e:  # noqa: BLE001
            return f"❌ Ошибка загрузки оптимального портфеля: {type(e).__name__}: {str(e)[:200]}"

    def handle_help(self) -> str:
        return (
            "📚 *Инвестиционные команды*\n\n"
            "/balance — балансы счетов и карт\n"
            "/operations [N] — операции за N дней (по умолчанию 7)\n"
            "/market SYMBOL — котировка акции (например /market SBER)\n"
            "/scan [N] — сканер акций MOEX (топ по метрикам)\n"
            "/bonds [N] — сканер облигаций MOEX (ОФЗ и корп)\n"
            "/risk — оценка рисков портфеля (VaR, CVaR)\n"
            "/optimal — оптимальное распределение портфеля\n"
            "/stress [scenario] — стресс-тестирование портфеля\n"
            "/free — сколько можно вложить\n"
            "/help_invest — эта справка"
        )



