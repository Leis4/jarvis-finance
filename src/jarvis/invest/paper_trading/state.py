from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Position:
    instrument_id: str
    quantity: int
    average_price: float
    market_value: float
    sector_name: str


@dataclass(slots=True)
class PortfolioState:
    asof: datetime
    cash: float
    equity: float
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
