from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from jarvis.finance.bank.base import BankAdapter
from jarvis.finance.domain import Transaction
from jarvis.finance.importers import load_transactions_csv


class CSVBankAdapter(BankAdapter):
    """Read-only bank-like adapter backed by a CSV export."""

    def __init__(self, csv_path: str | Path, account: str = "main"):
        self.csv_path = Path(csv_path)
        self.account = account

    def get_transactions(self) -> list[Transaction]:
        return load_transactions_csv(self.csv_path, account=self.account)

    def get_balance(self) -> Decimal:
        balance = sum((tx.amount for tx in self.get_transactions()), Decimal("0.00"))
        return balance.quantize(Decimal("0.01"))
