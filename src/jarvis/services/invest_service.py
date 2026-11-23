from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from jarvis.bridge.free_cash import capacity_preview, get_free_cash
from jarvis.finance.db import FinanceDB
from jarvis.finance.review_store import ReviewStore
from jarvis.mcp.adapters.moex import MoexMCP
from jarvis.mcp.adapters.sber import SberMCP, rub_account_subtotal
from jarvis.mcp.factory import get_moex_mcp, get_sber_mcp


class InvestService:
    """Facade for Sber + MOEX MCP with cached Sber session."""

    def __init__(
        self,
        sber: SberMCP | None = None,
        moex: MoexMCP | None = None,
        use_factory: bool | None = None,
    ) -> None:
        self._sber: SberMCP | None
        if sber is not None:
            self._sber = sber
        elif use_factory is False:
            self._sber = None
        else:
            sber_client = get_sber_mcp()
            self._sber = SberMCP(sber_client) if sber_client else None

        self._moex: MoexMCP | None
        if moex is not None:
            self._moex = moex
        elif use_factory is False:
            self._moex = None
        else:
            moex_client = get_moex_mcp()
            self._moex = MoexMCP(moex_client) if moex_client else None

        self._session_id: str | None = None
        self._session_opened_at: float = 0.0

    def _ensure_session(self) -> str:
        if self._sber is None:
            raise RuntimeError("Sber MCP not configured")
        self._session_id = self._sber.read_session()
        return self._session_id

    def sber_configured(self) -> bool:
        return self._sber is not None

    def moex_configured(self) -> bool:
        return self._moex is not None

    # ---- Sber: балансы ----

    def get_balances(self) -> dict[str, Any]:
        """Return one observed snapshot without asserting complete bank coverage."""
        if self._sber is None:
            return {"error": "Sber MCP not configured"}
        self._ensure_session()
        products = self._sber.get_products()
        try:
            total: str | None = str(rub_account_subtotal(products))
        except (ValueError, TypeError):
            total = None
        return {
            "accounts": products["accounts"],
            "cards": products["cards"],
            "total_rub": total,
            "status": "observed_only" if total is not None else "insufficient_data",
            "coverage": "unknown",
            "total_scope": "observed_rub_accounts_excluding_cards",
            "database_modified": False,
        }

    # ---- Sber: операции ----

    def get_operations(self, from_date: str, to_date: str, limit: int = 20, truncate: bool = True) -> list[dict[str, Any]]:
        if self._sber is None:
            raise RuntimeError("Sber MCP not configured")
        self._ensure_session()
        ops = self._sber.get_all_operations(from_date, to_date, limit_per_resource=limit)
        ops.sort(key=lambda x: str(x.get("date", "")), reverse=True)
        return ops[:limit] if truncate else ops

    # ---- MOEX: котировка ----

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        if self._moex is None:
            return {"error": "MOEX MCP not configured"}
        quote = self._moex.get_quote(symbol)
        history = self._moex.get_history(symbol)
        last_val = quote.get("LAST") or quote.get("LCLOSE")
        summary: dict[str, Any] = {
            "ticker": symbol.upper(),
            "last": float(last_val) if last_val is not None else None,
            "change_pct": quote.get("LASTCHANGEPRCNT"),
            "history": history[-5:] if history else [],
        }
        return summary

    # ---- Bridge: free cash ----

    def get_free_cash(self, db: FinanceDB, horizon_days: int = 30) -> float:
        return get_free_cash(db, horizon_days=horizon_days)

    def get_capacity_preview(
        self, store: ReviewStore, fact_ids: dict[str, str], *, start: date, end: date,
        currency: str, scenarios: dict[str, Decimal], now: datetime | None = None,
    ) -> dict[str, Any]:
        """Use accepted local facts without connecting Sber or MOEX."""
        return capacity_preview(store, fact_ids, start=start, end=end, currency=currency, scenarios=scenarios, now=now)

    # ---- MOEX: сканер ----

    def get_market_scan(self, top_n: int = 10) -> str:
        """Run stock scanner, return Telegram-ready report."""
        from jarvis.invest.scanner.report import render_stock_scan
        from jarvis.invest.scanner.stock_scanner import scan_stocks

        if self._moex is None:
            return "❌ MOEX MCP not configured"

        try:
            candidates = scan_stocks(self._moex, top_n=top_n)
        except Exception as e:  # noqa: BLE001
            return f"❌ Сканер недоступен ({type(e).__name__})."

        return render_stock_scan(candidates)

    def get_bond_scan(self, top_n: int = 10) -> str:
        """Run bond scanner, return Telegram-ready report."""
        from jarvis.invest.scanner.bond_scanner import scan_bonds

        if self._moex is None:
            return "❌ MOEX MCP not configured"

        try:
            bonds = scan_bonds(self._moex, top_n=top_n)
            if not bonds:
                return "📉 Облигации MOEX: данные не найдены"
            lines = [f"📉 Облигации MOEX (research, топ-{len(bonds)})", "Годовой купон / чистая цена; YTM и дюрация недоступны."]
            for i, b in enumerate(bonds, 1):
                lines.append(
                    f"{i}. {b['name']} ({b['ticker']}) — купонный proxy {b['annual_coupon_yield_pct']:.2f}%"
                )
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            return f"❌ Сканер облигаций недоступен ({type(e).__name__})."

    def get_stress_test(self, portfolio: dict[str, float], scenario: str = "covid_2020") -> str:
        """Run stress scenario on portfolio and return formatted report string."""
        from jarvis.invest.risk.stress import stress_test_scenario

        res = stress_test_scenario(portfolio, scenario)
        loss_val = res["portfolio_loss"]
        loss_pct = res["portfolio_loss_pct"] * 100
        worst = res["worst_asset"]
        days = res["recovery_estimate_days"]

        return (
            f"💥 {scenario.upper()}: портфель {loss_pct:.1f}% ({loss_val:+.0f} ₽). "
            f"Худший: {worst}. Ожидаемое восстановление: ~{days} дней."
        )
