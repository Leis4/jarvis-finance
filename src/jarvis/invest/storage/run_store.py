from __future__ import annotations

from typing import Any
from uuid import uuid4

from jarvis.invest.core.interfaces import OrderResult
from jarvis.invest.core.utils import utc_now
from jarvis.invest.db.models import (
    AuditLog,
    OrderRecord,
    ReconciliationRecord,
    RunRecord,
)
from jarvis.invest.db.session import session_scope


def create_run_record(run_type: str, status: str, metadata: dict[str, Any]) -> str:
    run_id = uuid4().hex
    with session_scope() as session:
        session.add(RunRecord(run_id=run_id, run_type=run_type, status=status, started_at=utc_now(), metadata_json=metadata))
    return run_id


def complete_run_record(run_id: str, status: str, artifact_uri: str | None = None) -> None:
    with session_scope() as session:
        record = session.get(RunRecord, run_id)
        if record is None:
            return
        record.status = status
        record.completed_at = utc_now()
        record.artifact_uri = artifact_uri


def append_audit(event_type: str, actor: str, payload: dict[str, Any]) -> str:
    event_id = uuid4().hex
    with session_scope() as session:
        session.add(AuditLog(event_id=event_id, event_type=event_type, actor=actor, created_at=utc_now(), payload=payload))
    return event_id


def append_order(order: OrderResult) -> None:
    with session_scope() as session:
        session.add(OrderRecord(order_id=order.order_id, broker_mode=order.broker_mode, instrument_id=order.instrument_id, side=order.side, quantity=order.quantity, requested_price=order.requested_price, filled_price=order.filled_price, status=order.status, created_at=order.submitted_at, metadata_json=order.metadata))


def append_reconciliation(broker_mode: str, asof_date: str, status: str, diff_summary: dict[str, Any], raw_path: str | None) -> str:
    reconciliation_id = uuid4().hex
    with session_scope() as session:
        session.add(ReconciliationRecord(reconciliation_id=reconciliation_id, broker_mode=broker_mode, asof_date=asof_date, status=status, diff_summary=diff_summary, raw_path=raw_path))
    return reconciliation_id
