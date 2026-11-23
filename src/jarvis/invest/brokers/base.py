from __future__ import annotations

from typing import Any, cast

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.interfaces import BrokerAdapter, OrderRequest, OrderResult
from jarvis.invest.paper_trading.engine import SimulatedPaperBroker
from jarvis.invest.storage.market_repository import LocalMarketDataRepository


class PaperBrokerAdapter(BrokerAdapter):
    def __init__(self) -> None:
        self.settings = get_settings()
        self.paper = SimulatedPaperBroker()
        self.repo = LocalMarketDataRepository()

    def _latest_market(self, asof: str | None = None) -> pd.DataFrame:
        market = self.repo.read_table("silver", "market").copy()
        market["effective_timestamp"] = pd.to_datetime(market["effective_timestamp"], utc=True)
        if asof is not None:
            asof_ts = pd.to_datetime(asof, utc=True)
            market = market[market["effective_timestamp"] <= asof_ts]
        return market.sort_values("effective_timestamp").groupby("instrument_id").tail(1)

    def get_accounts(self) -> list[dict[str, Any]]:
        return [{"account_id": "paper-main", "name": "Paper Trading", "type": "paper"}]

    def get_portfolio(self, account_id: str | None = None) -> dict[str, Any]:
        self.paper.mark_to_market(self._latest_market())
        return self.paper.current_portfolio()

    def get_positions(self, account_id: str | None = None) -> list[dict[str, Any]]:
        portfolio = self.paper.current_portfolio()["positions"]
        if isinstance(portfolio, dict):
            return [{"instrument_id": k, **v} for k, v in portfolio.items() if isinstance(v, dict)]
        return []

    def get_cash_balances(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return [{"currency": "RUB", "amount": self.paper.current_portfolio()["cash"]}]

    def get_instruments(self) -> list[dict[str, Any]]:
        market = self._latest_market()
        return cast(list[dict[str, Any]], market[["instrument_id", "ticker", "sector_name"]].drop_duplicates().to_dict(orient="records"))

    def get_quotes(self, instrument_ids: list[str]) -> list[dict[str, Any]]:
        market = self._latest_market()
        quotes = market[market["instrument_id"].isin(instrument_ids)]
        return cast(list[dict[str, Any]], quotes[["instrument_id", "close", "volume"]].to_dict(orient="records"))

    def place_order(self, order: OrderRequest) -> OrderResult:
        market_asof = order.metadata.get("market_asof")
        market = self._latest_market(str(market_asof) if market_asof is not None else None)
        return self.paper.submit_orders([order], market)[0]

    def cancel_order(self, order_id: str, account_id: str | None = None) -> dict[str, Any]:
        return {"order_id": order_id, "status": "not_supported_for_filled_paper_orders"}

    def get_orders(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_fills(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_operations(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def validate_order(self, order: OrderRequest) -> dict[str, Any]:
        return {"valid": order.quantity > 0, "reasons": [] if order.quantity > 0 else ["quantity_must_be_positive"]}

    def healthcheck(self) -> dict[str, Any]:
        return {"status": "ok", "mode": "paper"}
