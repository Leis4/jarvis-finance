from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from jarvis.finance.domain import Transaction


class BankAdapter(ABC):
    """Strict bank adapter interface.

    Implementations should never expose unrestricted banking access to the LLM.
    Money-moving methods must be explicit and guarded by user confirmation.
    """

    @abstractmethod
    def get_balance(self) -> Decimal:
        raise NotImplementedError

    @abstractmethod
    def get_transactions(self) -> list[Transaction]:
        raise NotImplementedError

    def prepare_transfer(self, amount: Decimal, source: str, destination: str) -> dict[str, str]:
        return {
            "status": "prepared_only",
            "amount": str(amount),
            "source": source,
            "destination": destination,
            "requires_confirmation": "true",
        }

    def execute_transfer(self, prepared_transfer_id: str, confirmed_by_user: bool) -> None:
        if not confirmed_by_user:
            raise PermissionError("Transfer execution requires explicit user confirmation")
        raise NotImplementedError("Real transfer execution is intentionally not implemented in v0.1")
