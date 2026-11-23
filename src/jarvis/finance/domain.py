from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class TransactionType(str, Enum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"


class ActionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Transaction:
    tx_date: date
    amount: Decimal
    currency: str
    description: str
    merchant: str
    category: str
    account: str = "main"
    external_id: str | None = None

    @property
    def type(self) -> TransactionType:
        if self.amount > Decimal(0):
            return TransactionType.INCOME
        if self.amount < Decimal(0):
            return TransactionType.EXPENSE
        return TransactionType.TRANSFER


@dataclass(frozen=True)
class BudgetLimit:
    category: str
    monthly_limit: Decimal


@dataclass(frozen=True)
class Goal:
    name: str
    target_amount: Decimal
    current_amount: Decimal = Decimal(0)


@dataclass(frozen=True)
class PurchaseAdvice:
    name: str
    amount: Decimal
    can_buy: bool
    risk_level: str
    message: str


@dataclass(frozen=True)
class ActionRequest:
    id: int
    action_type: str
    payload: dict[str, Any]
    status: ActionStatus
    reason: str
    approval_required: bool
    created_at: datetime
    updated_at: datetime
