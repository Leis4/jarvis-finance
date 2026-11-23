"""Database models for invest platform."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

# Dedicated Base for invest models (not mixed with finance models)
from jarvis.invest.db.base import Base


class RunRecord(Base):
    __tablename__ = "run_records"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artifact_uri: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderRecord(Base):
    __tablename__ = "order_records"

    order_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    broker_mode: Mapped[str] = mapped_column(String(32), index=True)
    instrument_id: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    requested_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ReconciliationRecord(Base):
    __tablename__ = "reconciliation_records"

    reconciliation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    broker_mode: Mapped[str] = mapped_column(String(32), index=True)
    asof_date: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    diff_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_path: Mapped[str | None] = mapped_column(Text, nullable=True)
