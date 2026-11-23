from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.db import FinanceDB


@dataclass(frozen=True)
class FoodPriceCandidate:
    item_name: str
    store: str
    unit: str
    price_per_unit: Decimal
    observed_date: str


@dataclass(frozen=True)
class ShoppingPlanRow:
    item_name: str
    daily_quantity: Decimal
    monthly_quantity: Decimal
    unit: str
    store: str
    price_per_unit: Decimal
    monthly_cost: Decimal


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def import_food_prices_csv(db: FinanceDB, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            item_name = (row.get("item_name") or row.get("name") or "").strip()
            if not item_name:
                continue
            db.add_food_price_observation(
                item_name=item_name,
                store=(row.get("store") or "").strip(),
                unit=(row.get("unit") or "kg").strip(),
                price_per_unit=money(row.get("price_per_unit") or row.get("price") or 0),
                observed_date=date.fromisoformat((row.get("observed_date") or row.get("date") or date.today().isoformat()).strip()),  # noqa: DTZ011
                notes=(row.get("notes") or "").strip(),
            )
            count += 1
    return count


def best_food_prices(db: FinanceDB) -> dict[str, FoodPriceCandidate]:
    best: dict[str, FoodPriceCandidate] = {}
    for row in db.list_food_price_observations(limit=10000):
        item = str(row["item_name"])
        candidate = FoodPriceCandidate(
            item_name=item,
            store=str(row["store"]),
            unit=str(row["unit"]),
            price_per_unit=money(row["price_per_unit"]),
            observed_date=str(row["observed_date"]),
        )
        current = best.get(item)
        if current is None or candidate.price_per_unit < current.price_per_unit:
            best[item] = candidate
    return best


def build_shopping_plan(db: FinanceDB, days: int = 30) -> list[ShoppingPlanRow]:
    best = best_food_prices(db)
    rows: list[ShoppingPlanRow] = []
    for item in db.list_food_items():
        name = str(item["name"])
        daily_quantity = Decimal(str(item["daily_quantity"]))
        monthly_quantity = (daily_quantity * Decimal(str(days))).quantize(Decimal("0.001"))
        candidate = best.get(name)
        if candidate is None:
            price = money(item["price_per_unit"])
            store = str(item.get("store") or "")
            unit = str(item["unit"])
        else:
            price = candidate.price_per_unit
            store = candidate.store
            unit = candidate.unit
        monthly_cost = money(price * monthly_quantity)
        rows.append(ShoppingPlanRow(name, daily_quantity, monthly_quantity, unit, store, price, monthly_cost))
    return rows


def render_shopping_plan(rows: list[ShoppingPlanRow]) -> str:
    total = sum((row.monthly_cost for row in rows), Decimal("0.00")).quantize(Decimal("0.01"))
    lines = [f"План закупки рациона: {total} RUB", ""]
    for row in rows:
        lines.append(
            f"- {row.item_name}: {row.monthly_quantity} {row.unit} * {row.price_per_unit} RUB, "
            f"магазин: {row.store or 'не указан'}, итого {row.monthly_cost} RUB"
        )
    return "\n".join(lines)
