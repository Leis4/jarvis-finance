from __future__ import annotations

import math

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.exceptions import RiskCheckError
from jarvis.invest.core.interfaces import OrderRequest, RiskEngine


class DefaultRiskEngine(RiskEngine):
    def __init__(self) -> None:
        self.settings = get_settings()

    @staticmethod
    def _finite(value: object) -> float:
        try:
            result = float(str(value))
        except (TypeError, ValueError, OverflowError):
            raise RiskCheckError("Finite numerical evidence required") from None
        if isinstance(value, bool) or not math.isfinite(result):
            raise RiskCheckError("Finite numerical evidence required")
        return result

    def _validate_inputs(self, orders: list[OrderRequest], portfolio: dict[str, object], market: pd.DataFrame) -> None:
        """Refuse unknown, malformed and nonfinite snapshots before numerical comparisons."""
        if portfolio.get("data_quality_ok") is not True or portfolio.get("broker_ok") is not True:
            raise RiskCheckError("Explicit snapshot quality and broker status required")
        if self._finite(portfolio.get("equity")) <= 0:
            raise RiskCheckError("Known positive equity required")
        if (not {"instrument_id", "close", "volume"} <= set(market.columns) or market.empty or
                market["instrument_id"].isna().any() or market["instrument_id"].duplicated().any()):
            raise RiskCheckError("Unique complete market snapshot required")
        for row in market.to_dict("records"):
            price, volume = self._finite(row["close"]), self._finite(row["volume"])
            if price <= 0 or volume < 0 or not math.isfinite(price * volume):
                raise RiskCheckError("Positive price and finite nonnegative volume required")
        raw = portfolio.get("positions")
        if raw is None:
            if type(portfolio.get("open_positions")) is not int or portfolio["open_positions"] != 0:
                raise RiskCheckError("Known positions or explicit empty holdings required")
            raw = {}
        if not isinstance(raw, (dict, list)):
            raise RiskCheckError("Positions must be an explicit mapping or list")
        records = list(raw.values()) if isinstance(raw, dict) else raw
        ids: set[str] = set()
        for position in records:
            if not isinstance(position, dict) or type(position.get("quantity")) is not int or position["quantity"] < 0:
                raise RiskCheckError("Known nonnegative integer position quantities required")
            if isinstance(raw, list):
                ident = position.get("instrument_id")
                if not isinstance(ident, str) or not ident or ident in ids:
                    raise RiskCheckError("Distinct known position identities required")
                ids.add(ident)
        for order in orders:
            if (type(order.quantity) is not int or order.quantity <= 0 or not isinstance(order.side, str) or
                    order.side.lower() not in {"buy", "sell"} or not isinstance(order.order_type, str) or
                    order.order_type.lower() not in {"market", "limit"}):
                raise RiskCheckError("Positive integer quantity and supported side/type required")
            if order.price is not None and self._finite(order.price) <= 0:
                raise RiskCheckError("Positive finite request price required")
            if order.order_type.lower() == "limit" and order.price is None:
                raise RiskCheckError("Limit request price required")

    @staticmethod
    def _normalize_positions(portfolio: dict[str, object]) -> dict[str, dict[str, object]]:
        raw_positions = portfolio.get("positions", {})
        if isinstance(raw_positions, dict):
            return {
                instrument_id: {
                    "quantity": int(payload.get("quantity", 0)),
                    "average_price": float(payload.get("average_price", 0.0)),
                    "sector_name": str(payload.get("sector_name", "UNKNOWN")),
                }
                for instrument_id, payload in raw_positions.items()
                if isinstance(payload, dict)
            }
        if isinstance(raw_positions, list):
            normalized: dict[str, dict[str, object]] = {}
            for payload in raw_positions:
                if not isinstance(payload, dict) or "instrument_id" not in payload:
                    continue
                normalized[str(payload["instrument_id"])] = {
                    "quantity": int(payload.get("quantity", 0)),
                    "average_price": float(payload.get("average_price", 0.0)),
                    "sector_name": str(payload.get("sector_name", "UNKNOWN")),
                }
            return normalized
        return {}

    def validate_orders(self, orders: list[OrderRequest], portfolio: dict[str, object], market: pd.DataFrame) -> list[OrderRequest]:
        if not orders:
            return []
        self._validate_inputs(orders, portfolio, market)
        approved: list[OrderRequest] = []
        lookup = market.set_index("instrument_id")
        positions = self._normalize_positions(portfolio)
        if not portfolio.get("data_quality_ok", True):
            raise RiskCheckError("Trading halted: data quality failure.")
        if not portfolio.get("broker_ok", True):
            raise RiskCheckError("Trading halted: broker failure.")
        equity = self._finite(portfolio["equity"])
        max_position_pct = self._finite(portfolio.get("max_position_pct", self.settings.risk_max_position_pct))
        max_sector_pct = self._finite(portfolio.get("max_sector_pct", self.settings.risk_max_sector_exposure))
        max_positions = portfolio.get("max_concurrent_positions", self.settings.risk_max_concurrent_positions)
        if type(max_positions) is not int:
            raise RiskCheckError("Positive integer position limit required")
        max_concurrent_positions = int(str(max_positions))
        max_gross_exposure = self._finite(portfolio.get("max_gross_exposure", self.settings.risk_max_gross_exposure))
        max_daily_turnover = self._finite(portfolio.get("max_daily_turnover", self.settings.risk_max_daily_turnover))
        limits = [max_position_pct, max_sector_pct, max_gross_exposure, max_daily_turnover,
                  self.settings.risk_min_daily_dollar_volume, self.settings.risk_liquidity_fraction]
        if (any(not math.isfinite(value) or value < 0 or not math.isfinite(equity * value) for value in limits) or
                not 0 < max_position_pct <= 1 or not 0 < max_sector_pct <= 1 or
                max_concurrent_positions <= 0 or not 0 < self.settings.risk_liquidity_fraction <= 1):
            raise RiskCheckError("Finite nonnegative risk limits required")
        projected_quantities: dict[str, int] = {}
        projected_sector_notional: dict[str, float] = {}
        for instrument_id, payload in positions.items():
            projected_quantities[instrument_id] = int(str(payload.get("quantity", 0)))
            if projected_quantities[instrument_id] > 0 and instrument_id not in lookup.index:
                raise RiskCheckError("Held position quote unavailable; exposure cannot be estimated")
            if instrument_id in lookup.index:
                quote = lookup.loc[instrument_id]
                sec_key = str(quote.get("sector_name", payload.get("sector_name", "UNKNOWN")))
                projected_sector_notional[sec_key] = projected_sector_notional.get(sec_key, 0.0) + projected_quantities[instrument_id] * float(quote["close"])
        turnover_notional = 0.0
        for order in orders:
            if order.instrument_id not in lookup.index:
                raise RiskCheckError(f"Missing market data for {order.instrument_id}.")
            quote = lookup.loc[order.instrument_id]
            last_price = float(quote["close"])
            daily_dollar_volume = float(quote["close"] * quote["volume"])
            if daily_dollar_volume < self.settings.risk_min_daily_dollar_volume:
                raise RiskCheckError(f"Liquidity threshold breach for {order.instrument_id}.")
            if order.quantity > quote["volume"] * self.settings.risk_liquidity_fraction:
                raise RiskCheckError(f"Participation too high for {order.instrument_id}.")
            turnover_notional += order.quantity * last_price
            sector = str(quote.get("sector_name", "UNKNOWN"))
            current_quantity = projected_quantities.get(order.instrument_id, 0)
            current_sector_notional = projected_sector_notional.get(sector, 0.0)
            if order.side.lower() == "buy":
                post_quantity = current_quantity + order.quantity
                post_notional = post_quantity * last_price
                if post_notional > equity * max_position_pct:
                    raise RiskCheckError(f"Position size breach for {order.instrument_id}.")
                if current_sector_notional + order.quantity * last_price > equity * max_sector_pct:
                    raise RiskCheckError(f"Sector exposure limit exceeded for {sector}.")
                projected_quantities[order.instrument_id] = post_quantity
                projected_sector_notional[sector] = float(current_sector_notional + order.quantity * last_price)
            else:
                if order.quantity > current_quantity:
                    raise RiskCheckError(f"Cannot sell more than held for {order.instrument_id}.")
                projected_quantities[order.instrument_id] = current_quantity - order.quantity
                projected_sector_notional[sector] = float(max(0.0, current_sector_notional - order.quantity * last_price))
            approved.append(order)
        open_positions = sum(1 for quantity in projected_quantities.values() if quantity > 0)
        if open_positions > max_concurrent_positions:
            raise RiskCheckError("Maximum concurrent positions exceeded.")
        gross_exposure = 0.0
        for instrument_id, quantity in projected_quantities.items():
            if quantity <= 0:
                continue
            if instrument_id in lookup.index:
                gross_exposure += quantity * float(lookup.loc[instrument_id]["close"])
        if gross_exposure > equity * max_gross_exposure:
            raise RiskCheckError("Gross exposure limit exceeded.")
        if not all(math.isfinite(v) for v in [gross_exposure, turnover_notional, *projected_sector_notional.values()]):
            raise RiskCheckError("Risk notional overflowed")
        turnover = turnover_notional / equity
        if turnover > max_daily_turnover:
            raise RiskCheckError("Daily turnover limit exceeded.")
        return approved
