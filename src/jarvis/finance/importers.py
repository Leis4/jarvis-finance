from __future__ import annotations

import csv
import hashlib
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from jarvis.finance.domain import Transaction

CATEGORY_KEYWORDS = {
    "food": ["food", "grocery", "groceries", "lenta", "perekrestok", "магнит", "пятерочка", "еда", "продукты"],
    "subscriptions": ["subscription", "plus", "netflix", "spotify", "подписка"],
    "transport": ["taxi", "metro", "go", "transport", "такси", "метро"],
    "entertainment": ["cinema", "game", "kino", "развлеч", "кино"],
    "shopping": ["dns", "ozon", "wildberries", "market", "keyboard", "monitor", "покупка"],
    "housing": ["rent", "landlord", "аренда", "квартира"],
    "income": ["salary", "payroll", "зарплата", "аванс"],
}


def infer_category(description: str, merchant: str, amount: Decimal) -> str:
    if amount > 0:
        return "income"
    text = f"{description} {merchant}".lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return "uncategorized"


def _external_id(row: dict[str, str], account: str) -> str:
    raw = "|".join([account, row.get("date", ""), row.get("amount", ""), row.get("description", ""), row.get("merchant", "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def load_transactions_csv(path: str | Path, account: str = "main") -> list[Transaction]:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"CSV file not found: {source}")

    transactions: list[Transaction] = []
    with source.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        required = {"date", "amount"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
        for row in reader:
            try:
                amount = Decimal(str(row["amount"]).replace(",", ".")).quantize(Decimal("0.01"))
            except (InvalidOperation, KeyError) as exc:
                raise ValueError(f"Invalid amount in row: {row}") from exc

            tx_date = date.fromisoformat(row["date"])
            currency = (row.get("currency") or "RUB").strip().upper()
            description = (row.get("description") or "").strip()
            merchant = (row.get("merchant") or "").strip()
            category = (row.get("category") or "").strip().lower() or infer_category(description, merchant, amount)
            transactions.append(
                Transaction(
                    tx_date=tx_date,
                    amount=amount,
                    currency=currency,
                    description=description,
                    merchant=merchant,
                    category=category,
                    account=account,
                    external_id=_external_id(row, account),
                )
            )
    return transactions
