from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.rules import DEFAULT_RULES, decimal_money

CATEGORIES = [
    "food",
    "entertainment",
    "subscriptions",
    "transport",
    "shopping",
    "delivery",
    "health",
    "uncategorized",
]
FLEXIBLE_CATEGORIES = {"entertainment", "shopping", "delivery", "subscriptions", "wants"}
MODEL_FILE = "spend_predictor.pt"
METADATA_FILE = "spend_predictor_meta.json"


def torch_available() -> bool:
    try:
        import torch  # type: ignore[import-not-found] # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class TrainingResult:
    engine: str
    trained: bool
    samples: int
    model_path: str | None
    message: str


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def round_to_100(value: Decimal) -> Decimal:
    rounded = (value / Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP) * Decimal(100)
    return rounded.quantize(Decimal("0.01"))


def month_key_from_date(date_s: str) -> str:
    return date_s[:7]


def next_month_key(month: str) -> str:
    y, m = [int(part) for part in month.split("-", 1)]
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


def category_index(category: str) -> int:
    if category in CATEGORIES:
        return CATEGORIES.index(category)
    return CATEGORIES.index("uncategorized")


def category_month_totals(db: FinanceDB) -> dict[tuple[str, str], Decimal]:
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0.00"))
    for tx in db.all_transactions():
        amount = Decimal(str(tx["amount"]))
        if amount < 0:
            category = tx.get("category") or "uncategorized"
            totals[(month_key_from_date(tx["tx_date"]), category)] += abs(amount)

    # Manual category entries are treated as explicit factual spending inputs.
    # This lets the user enter real-world spending even before bank import is connected.
    for entry in db.list_category_spending_entries(limit=10000):
        totals[(month_key_from_date(entry["entry_date"]), entry["category"])] += Decimal(str(entry["amount_spent"]))

    return {key: value.quantize(Decimal("0.01")) for key, value in totals.items()}


def latest_month(totals: dict[tuple[str, str], Decimal]) -> str | None:
    months = sorted({month for month, _category in totals})
    return months[-1] if months else None


def _feature_vector(category: str, month: str, previous_spend: Decimal, limit_amount: Decimal) -> list[float]:
    _year, month_s = month.split("-", 1)
    month_i = int(month_s)
    cat_norm = category_index(category) / max(1, len(CATEGORIES) - 1)
    sin_m = math.sin(2 * math.pi * month_i / 12)
    cos_m = math.cos(2 * math.pi * month_i / 12)
    prev_norm = float(previous_spend) / 10000.0
    limit_norm = float(limit_amount) / 10000.0
    return [cat_norm, sin_m, cos_m, prev_norm, limit_norm]


def build_training_rows(db: FinanceDB, rules: dict[str, Any]) -> tuple[list[list[float]], list[float]]:
    totals = category_month_totals(db)
    if not totals:
        return [], []
    months = sorted({month for month, _category in totals})
    limits = rules.get("category_limits", DEFAULT_RULES["category_limits"])
    x_rows: list[list[float]] = []
    y_rows: list[float] = []
    for i in range(1, len(months)):
        prev_month = months[i - 1]
        current_month = months[i]
        for category in sorted(set(limits) | set(CATEGORIES)):
            prev_spend = totals.get((prev_month, category), Decimal("0.00"))
            target_spend = totals.get((current_month, category), Decimal("0.00"))
            if prev_spend == 0 and target_spend == 0:
                continue
            limit_amount = decimal_money(limits.get(category, 0))
            x_rows.append(_feature_vector(category, current_month, prev_spend, limit_amount))
            y_rows.append(float(target_spend) / 10000.0)
    return x_rows, y_rows


def train_spend_predictor(db: FinanceDB, rules: dict[str, Any], model_dir: str | Path, epochs: int = 250) -> TrainingResult:
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    x_rows, y_rows = build_training_rows(db, rules)
    if len(x_rows) < 4:
        return TrainingResult(
            engine="fallback",
            trained=False,
            samples=len(x_rows),
            model_path=None,
            message="Not enough month-to-month data for PyTorch training. Need at least 4 training samples.",
        )

    if not torch_available():
        return TrainingResult(
            engine="fallback",
            trained=False,
            samples=len(x_rows),
            model_path=None,
            message="PyTorch is not installed. Install optional dependency: pip install -e .[ml]",
        )

    import torch
    from torch import nn

    torch.manual_seed(42)
    x = torch.tensor(x_rows, dtype=torch.float32)
    y = torch.tensor(y_rows, dtype=torch.float32).view(-1, 1)
    model = SpendNet(input_size=x.shape[1])
    optimizer = torch.optim.Adam(model.parameters(), lr=0.03)
    loss_fn = nn.MSELoss()

    model.train()
    for _ in range(max(1, epochs)):
        optimizer.zero_grad()
        prediction = model(x)
        loss = loss_fn(prediction, y)
        loss.backward()
        optimizer.step()

    model_path = model_dir / MODEL_FILE
    meta_path = model_dir / METADATA_FILE
    torch.save(model.state_dict(), model_path)
    meta_path.write_text(
        json.dumps(
            {
                "categories": CATEGORIES,
                "input_size": int(x.shape[1]),
                "target_scale": 10000,
                "feature_order": ["category_norm", "month_sin", "month_cos", "previous_spend_norm", "limit_norm"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    db.set_setting("ml_model_dir", str(model_dir))
    return TrainingResult(
        engine="pytorch",
        trained=True,
        samples=len(x_rows),
        model_path=str(model_path),
        message=f"PyTorch spend predictor trained on {len(x_rows)} samples.",
    )


if torch_available():
    from torch import nn

    class SpendNet(nn.Module):  # type: ignore[misc]
        def __init__(self, input_size: int = 5):
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(input_size, 16),
                nn.ReLU(),
                nn.Linear(16, 8),
                nn.ReLU(),
                nn.Linear(8, 1),
                nn.Softplus(),
            )

        def forward(self, x: Any) -> Any:
            return self.net(x)
else:
    class SpendNet:  # type: ignore[no-redef]
        pass


def _load_model(model_dir: str | Path) -> Any:
    if not torch_available():
        return None
    import torch

    model_path = Path(model_dir) / MODEL_FILE
    if not model_path.exists():
        return None
    model = SpendNet(input_size=5)
    state = torch.load(model_path, map_location="cpu")
    model.load_state_dict(state)
    model.eval()
    return model


def _predict_with_model(model: Any, feature: list[float]) -> Decimal:
    import torch

    with torch.no_grad():
        x = torch.tensor([feature], dtype=torch.float32)
        raw = float(model(x).item()) * 10000.0
    return money(max(0.0, raw))


def fallback_prediction(previous_spend: Decimal, current_limit: Decimal, category: str) -> Decimal:
    if previous_spend <= 0:
        return money(current_limit * Decimal("0.50")) if current_limit > 0 else Decimal("0.00")
    if category in FLEXIBLE_CATEGORIES:
        return money(previous_spend * Decimal("0.92"))
    return money(previous_spend * Decimal("0.98"))


def minimum_limit_for_category(category: str, rules: dict[str, Any], ration_monthly_cost: Decimal) -> Decimal:
    minimums = rules.get(
        "category_minimums",
        {
            "food": 3000,
            "transport": 1500,
            "subscriptions": 0,
            "entertainment": 500,
            "shopping": 500,
        },
    )
    configured = decimal_money(minimums.get(category, 0))
    if category == "food" and ration_monthly_cost > 0:
        return max(configured, money(ration_monthly_cost * Decimal("1.10")))
    return configured


def recommend_limit(category: str, predicted_spend: Decimal, current_limit: Decimal, rules: dict[str, Any], ration_monthly_cost: Decimal) -> Decimal:
    floor = minimum_limit_for_category(category, rules, ration_monthly_cost)
    factor = Decimal("0.85") if category in FLEXIBLE_CATEGORIES else Decimal("0.85")  # noqa: RUF034
    raw = predicted_spend * factor
    if current_limit > 0:
        raw = min(raw, current_limit)
    recommended = max(floor, raw)
    if current_limit > 0:
        recommended = min(current_limit, recommended)
    return round_to_100(recommended)


def build_spend_forecast(db: FinanceDB, rules: dict[str, Any], model_dir: str | Path | None = None) -> dict[str, Any]:
    totals = category_month_totals(db)
    lm = latest_month(totals)
    limits = rules.get("category_limits", DEFAULT_RULES["category_limits"])
    if model_dir is None:
        model_dir = db.get_setting("ml_model_dir", "./models")
    model = _load_model(model_dir)
    engine = "pytorch" if model is not None else "fallback"
    if lm is None:
        lm = date.today().strftime("%Y-%m")  # noqa: DTZ011
    forecast_month = next_month_key(lm)
    ration_daily = db.ration_daily_cost()
    ration_monthly = money(ration_daily * Decimal(30))

    categories = sorted(set(limits) | {category for _month, category in totals})
    rows: list[dict[str, Any]] = []
    total_predicted = Decimal("0.00")
    total_recommended = Decimal("0.00")
    total_savings = Decimal("0.00")
    for category in categories:
        previous_spend = totals.get((lm, category), Decimal("0.00"))
        current_limit = decimal_money(limits.get(category, 0))
        if model is not None:
            prediction = _predict_with_model(model, _feature_vector(category, forecast_month, previous_spend, current_limit))
        else:
            prediction = fallback_prediction(previous_spend, current_limit, category)
        recommended = recommend_limit(category, prediction, current_limit, rules, ration_monthly)
        suggested_savings = max(Decimal("0.00"), current_limit - recommended).quantize(Decimal("0.01"))
        total_predicted += prediction
        total_recommended += recommended
        total_savings += suggested_savings
        rows.append(
            {
                "category": category,
                "latest_month_spent": previous_spend.quantize(Decimal("0.01")),
                "predicted_spend": prediction.quantize(Decimal("0.01")),
                "current_limit": current_limit,
                "recommended_limit": recommended,
                "suggested_savings": suggested_savings,
                "note": build_category_note(category, previous_spend, prediction, recommended, ration_monthly),
            }
        )

    return {
        "engine": engine,
        "latest_month": lm,
        "forecast_month": forecast_month,
        "ration_daily_cost": ration_daily,
        "ration_monthly_cost": ration_monthly,
        "total_predicted_spend": total_predicted.quantize(Decimal("0.01")),
        "total_recommended_limits": total_recommended.quantize(Decimal("0.01")),
        "total_suggested_savings": total_savings.quantize(Decimal("0.01")),
        "categories": rows,
    }


def build_category_note(
    category: str,
    previous_spend: Decimal,
    prediction: Decimal,
    recommended: Decimal,
    ration_monthly_cost: Decimal,
) -> str:
    if category == "food" and ration_monthly_cost > 0:
        return "Лимит учитывает базу цен рациона: ниже расчетной стоимости питания опускать нельзя."
    if category in FLEXIBLE_CATEGORIES and recommended < prediction:
        return "Гибкая категория: рекомендуется ужать лимит и переводить разницу в резерв/накопления."
    if previous_spend == 0 and prediction == 0:
        return "Недостаточно данных; лимит сохранен консервативно."
    return "Прогноз построен по последним фактическим тратам и текущим лимитам."


def render_spend_forecast(forecast: dict[str, Any]) -> str:
    lines = [
        f"ML-прогноз: {forecast['forecast_month']} (engine={forecast['engine']})",
        f"Последний месяц данных: {forecast['latest_month']}",
        f"Прогноз расходов: {forecast['total_predicted_spend']} RUB",
        f"Рекомендованные лимиты: {forecast['total_recommended_limits']} RUB",
        f"Потенциальная экономия по лимитам: {forecast['total_suggested_savings']} RUB",
        f"Рацион: {forecast['ration_daily_cost']} RUB/день, примерно {forecast['ration_monthly_cost']} RUB/мес",
        "",
        "Категории:",
    ]
    for row in forecast["categories"]:
        lines.append(
            f"- {row['category']}: было {row['latest_month_spent']} | прогноз {row['predicted_spend']} | "
            f"лимит {row['current_limit']} -> {row['recommended_limit']} | экономия {row['suggested_savings']}"
        )
        lines.append(f"  {row['note']}")
    return "\n".join(lines)
