from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.interfaces import BrokerAdapter, OrderRequest, OrderResult
from jarvis.invest.core.utils import utc_now
from jarvis.invest.storage.run_store import append_reconciliation


class SberManualAdapter(BrokerAdapter):
    def __init__(self) -> None:
        self.settings = get_settings()
        self.outbox = self.settings.artifacts_dir / "sber_outbox"
        self.reconciliation_dir = self.settings.artifacts_dir / "sber_reconciliation"
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.reconciliation_dir.mkdir(parents=True, exist_ok=True)

    def get_accounts(self) -> list[dict[str, Any]]:
        return [{"account_id": "sber-manual", "name": "Sber Manual Workflow", "type": "manual"}]

    def get_portfolio(self, account_id: str | None = None) -> dict[str, Any]:
        return {"account_id": account_id or "sber-manual", "positions": [], "cash": None, "note": "External broker statement required."}

    def get_positions(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_cash_balances(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_instruments(self) -> list[dict[str, Any]]:
        return []

    def get_quotes(self, instrument_ids: list[str]) -> list[dict[str, Any]]:
        return [{"instrument_id": iid, "status": "external_quote_required"} for iid in instrument_ids]

    def _write_exports(self, order: OrderRequest) -> tuple[Path, Path]:
        timestamp = utc_now().strftime("%Y%m%d%H%M%S")
        base_name = f"{timestamp}_{order.instrument_id}_{order.side}"
        json_path = self.outbox / f"{base_name}.json"
        csv_path = self.outbox / f"{base_name}.csv"
        payload = {"instrument_id": order.instrument_id, "side": order.side, "quantity": order.quantity, "order_type": order.order_type, "price": order.price, "broker_mode": "sber_manual", "metadata": order.metadata}
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(payload.keys()))
            writer.writeheader()
            writer.writerow(payload)
        return json_path, csv_path

    def place_order(self, order: OrderRequest) -> OrderResult:
        json_path, csv_path = self._write_exports(order)
        return OrderResult(order_id=uuid4().hex, status="proposed_manual_submission", instrument_id=order.instrument_id, side=order.side, quantity=order.quantity, requested_price=order.price, filled_price=None, filled_quantity=0, broker_mode="sber_manual", submitted_at=utc_now(), metadata={"json_path": str(json_path), "csv_path": str(csv_path)})

    def cancel_order(self, order_id: str, account_id: str | None = None) -> dict[str, Any]:
        return {"order_id": order_id, "status": "manual_cancel_required"}

    def get_orders(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return [{"path": str(path)} for path in sorted(self.outbox.glob("*.json"))]

    def get_fills(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_operations(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def validate_order(self, order: OrderRequest) -> dict[str, Any]:
        reasons = []
        if order.quantity <= 0:
            reasons.append("quantity_must_be_positive")
        if order.side.lower() not in {"buy", "sell"}:
            reasons.append("unsupported_side")
        return {"valid": not reasons, "reasons": reasons}

    def healthcheck(self) -> dict[str, Any]:
        return {"status": "ok", "mode": "sber_manual", "outbox_dir": str(self.outbox)}

    def build_reconciliation_template(self, proposed_orders: list[OrderResult], asof_date: str) -> str:
        path = self.reconciliation_dir / f"reconciliation_{asof_date}.csv"
        frame = pd.DataFrame([{"order_id": o.order_id, "instrument_id": o.instrument_id, "side": o.side, "proposed_quantity": o.quantity, "proposed_price": o.requested_price, "broker_reported_quantity": "", "broker_reported_price": "", "status": "pending"} for o in proposed_orders])
        frame.to_csv(path, index=False)
        append_reconciliation("sber_manual", asof_date, "pending", {"count": len(proposed_orders)}, str(path))
        return str(path)
