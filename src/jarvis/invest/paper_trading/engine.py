from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.interfaces import OrderRequest, OrderResult, PaperBroker
from jarvis.invest.core.utils import utc_now
from jarvis.invest.paper_trading.state import PortfolioState, Position


class SimulatedPaperBroker(PaperBroker):
    def __init__(
        self,
        *,
        output_dir: Path | None = None,
        persist_state: bool = True,
        initial_state: PortfolioState | None = None,
    ) -> None:
        self.settings = get_settings()
        self.persist_state = persist_state
        self.output_dir = output_dir or (self.settings.artifacts_dir / "paper")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if initial_state is not None:
            self.state = initial_state
        elif self.persist_state:
            self.state = self._load_state()
        else:
            self.state = self._initial_state()

    def _initial_state(self) -> PortfolioState:
        now = utc_now()
        return PortfolioState(asof=now, cash=self.settings.paper_initial_cash, equity=self.settings.paper_initial_cash)

    def _latest_snapshot_path(self) -> Path | None:
        snapshots = sorted(self.output_dir.glob("snapshot_*.json"))
        return snapshots[-1] if snapshots else None

    def _load_state(self) -> PortfolioState:
        latest = self._latest_snapshot_path()
        if latest is None:
            return self._initial_state()
        payload = json.loads(latest.read_text(encoding="utf-8"))
        positions = {
            instrument_id: Position(
                instrument_id=instrument_id,
                quantity=int(item.get("quantity", 0)),
                average_price=float(item.get("average_price", 0.0)),
                market_value=float(item.get("market_value", 0.0)),
                sector_name=str(item.get("sector_name", "UNKNOWN")),
            )
            for instrument_id, item in payload.get("positions", {}).items()
        }
        return PortfolioState(
            asof=pd.to_datetime(payload["asof"], utc=True).to_pydatetime(),
            cash=float(payload.get("cash", self.settings.paper_initial_cash)),
            equity=float(payload.get("equity", self.settings.paper_initial_cash)),
            realized_pnl=float(payload.get("realized_pnl", 0.0)),
            fees_paid=float(payload.get("fees_paid", 0.0)),
            positions=positions,
        )

    def _fill_price(self, reference_price: float, side: str) -> float:
        bps = self.settings.paper_slippage_bps / 10_000
        return reference_price * (1 + bps if side.lower() == "buy" else 1 - bps)

    @staticmethod
    def _submitted_at(order: OrderRequest) -> Any:
        raw_value = order.metadata.get("execution_timestamp")
        if raw_value is None:
            return utc_now()
        return pd.to_datetime(raw_value, utc=True).to_pydatetime()

    def _refresh_market_values(self, market: pd.DataFrame) -> None:
        lookup = market.set_index("instrument_id")
        total_market_value = 0.0
        for instrument_id in list(self.state.positions):
            position = self.state.positions[instrument_id]
            if position.quantity <= 0:
                self.state.positions.pop(instrument_id, None)
                continue
            if instrument_id in lookup.index:
                quote = lookup.loc[instrument_id]
                position.market_value = position.quantity * float(quote["close"])
                position.sector_name = str(quote.get("sector_name", position.sector_name))
            else:
                position.market_value = position.quantity * position.average_price
            total_market_value += position.market_value
        self.state.equity = self.state.cash + total_market_value

    def _snapshot_payload(self, results: list[OrderResult]) -> dict[str, object]:
        return {
            "asof": self.state.asof.isoformat(),
            "cash": self.state.cash,
            "equity": self.state.equity,
            "realized_pnl": self.state.realized_pnl,
            "fees_paid": self.state.fees_paid,
            "positions": {
                instrument_id: {
                    "quantity": position.quantity,
                    "average_price": position.average_price,
                    "market_value": position.market_value,
                    "sector_name": position.sector_name,
                }
                for instrument_id, position in self.state.positions.items()
            },
            "fills": [
                {
                    "order_id": result.order_id,
                    "instrument_id": result.instrument_id,
                    "side": result.side,
                    "quantity": result.quantity,
                    "filled_price": result.filled_price,
                    "status": result.status,
                }
                for result in results
            ],
        }

    def _save_snapshot(self, results: list[OrderResult]) -> None:
        if not self.persist_state:
            return
        payload = self._snapshot_payload(results)
        path = self.output_dir / f"snapshot_{self.state.asof.strftime('%Y%m%d%H%M%S')}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def mark_to_market(self, market: pd.DataFrame) -> None:
        self._refresh_market_values(market)

    def submit_orders(self, orders: list[OrderRequest], market: pd.DataFrame) -> list[OrderResult]:
        lookup = market.set_index("instrument_id")
        results: list[OrderResult] = []
        fee_rate = self.settings.paper_fee_bps / 10_000
        for order in orders:
            if order.instrument_id not in lookup.index:
                results.append(OrderResult(order_id=uuid4().hex, status="rejected_missing_quote", instrument_id=order.instrument_id, side=order.side, quantity=order.quantity, requested_price=order.price, filled_price=None, filled_quantity=0, broker_mode="paper", submitted_at=self._submitted_at(order), metadata={}))
                continue
            quote = lookup.loc[order.instrument_id]
            fill_price = self._fill_price(float(quote["close"]), order.side)
            gross = fill_price * order.quantity
            fee = gross * fee_rate
            submitted_at = self._submitted_at(order)
            if order.side.lower() == "buy":
                if gross + fee > self.state.cash:
                    results.append(OrderResult(order_id=uuid4().hex, status="rejected_insufficient_cash", instrument_id=order.instrument_id, side=order.side, quantity=order.quantity, requested_price=order.price, filled_price=None, filled_quantity=0, broker_mode="paper", submitted_at=submitted_at, metadata={"required_cash": gross + fee, "available_cash": self.state.cash}))
                    continue
                self.state.cash -= gross + fee
                pos = self.state.positions.get(order.instrument_id, Position(order.instrument_id, 0, 0.0, 0.0, str(quote.get("sector_name", "UNKNOWN"))))
                new_qty = pos.quantity + order.quantity
                pos.average_price = ((pos.average_price * pos.quantity) + gross) / max(1, new_qty)
                pos.quantity = new_qty
                pos.market_value = new_qty * float(quote["close"])
                self.state.positions[order.instrument_id] = pos
            else:
                existing_pos = self.state.positions.get(order.instrument_id)
                if existing_pos is None or existing_pos.quantity < order.quantity:
                    results.append(OrderResult(order_id=uuid4().hex, status="rejected_insufficient_position", instrument_id=order.instrument_id, side=order.side, quantity=order.quantity, requested_price=order.price, filled_price=None, filled_quantity=0, broker_mode="paper", submitted_at=submitted_at, metadata={"available_quantity": 0 if existing_pos is None else existing_pos.quantity}))
                    continue
                self.state.cash += gross - fee
                self.state.realized_pnl += (fill_price - existing_pos.average_price) * order.quantity - fee
                existing_pos.quantity -= order.quantity
                if existing_pos.quantity == 0:
                    self.state.positions.pop(order.instrument_id, None)
                else:
                    existing_pos.market_value = existing_pos.quantity * float(quote["close"])
            self.state.asof = submitted_at
            self.state.fees_paid += fee
            results.append(OrderResult(order_id=uuid4().hex, status="filled", instrument_id=order.instrument_id, side=order.side, quantity=order.quantity, requested_price=order.price, filled_price=fill_price, filled_quantity=order.quantity, broker_mode="paper", submitted_at=self.state.asof, metadata={"fee": fee}))
        self._refresh_market_values(market)
        self._save_snapshot(results)
        return results

    def current_portfolio(self) -> dict[str, object]:
        return {
            "asof": self.state.asof.isoformat(),
            "cash": self.state.cash,
            "equity": self.state.equity,
            "realized_pnl": self.state.realized_pnl,
            "fees_paid": self.state.fees_paid,
            "positions": {
                instrument_id: {
                    "quantity": position.quantity,
                    "average_price": position.average_price,
                    "market_value": position.market_value,
                    "sector_name": position.sector_name,
                }
                for instrument_id, position in self.state.positions.items()
            },
        }
