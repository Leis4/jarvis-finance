from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.interfaces import BrokerAdapter, OrderRequest, OrderResult
from jarvis.invest.core.utils import utc_now


class SberSemiAutoAdapter(BrokerAdapter):
    def __init__(self) -> None:
        self.settings = get_settings()
        self.queue_dir = self.settings.artifacts_dir / "sber_semi_auto_queue"
        self.queue_dir.mkdir(parents=True, exist_ok=True)

    def get_accounts(self) -> list[dict[str, Any]]:
        return [{"account_id": "sber-semi-auto", "name": "Sber Semi-Auto", "type": "semi_auto"}]

    def get_portfolio(self, account_id: str | None = None) -> dict[str, Any]:
        return {"account_id": account_id or "sber-semi-auto", "status": "external_sync_required"}

    def get_positions(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_cash_balances(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_instruments(self) -> list[dict[str, Any]]:
        return []

    def get_quotes(self, instrument_ids: list[str]) -> list[dict[str, Any]]:
        return [{"instrument_id": iid, "status": "needs_provider"} for iid in instrument_ids]

    def place_order(self, order: OrderRequest) -> OrderResult:
        intent_id = str(uuid4().hex)
        payload = {"intent_id": intent_id, "instrument_id": order.instrument_id, "side": order.side, "quantity": order.quantity, "order_type": order.order_type, "price": order.price, "metadata": order.metadata, "created_at": utc_now().isoformat()}
        path = self.queue_dir / f"{intent_id}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return OrderResult(order_id=intent_id, status="queued_for_operator_or_api_bridge", instrument_id=order.instrument_id, side=order.side, quantity=order.quantity, requested_price=order.price, filled_price=None, filled_quantity=0, broker_mode="sber_semi_auto", submitted_at=utc_now(), metadata={"queue_path": str(path)})

    def cancel_order(self, order_id: str, account_id: str | None = None) -> dict[str, Any]:
        return {"order_id": order_id, "status": "cancellation_requires_external_bridge"}

    def get_orders(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return [{"intent_path": str(path)} for path in sorted(self.queue_dir.glob("*.json"))]

    def get_fills(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_operations(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def validate_order(self, order: OrderRequest) -> dict[str, Any]:
        reasons = []
        if order.quantity <= 0:
            reasons.append("quantity_must_be_positive")
        return {"valid": not reasons, "reasons": reasons}

    def healthcheck(self) -> dict[str, Any]:
        return {"status": "ok", "mode": "sber_semi_auto", "queue_dir": str(self.queue_dir)}
