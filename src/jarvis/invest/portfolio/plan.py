from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.exceptions import ConfigurationError


class PortfolioPlan(BaseModel):
    monthly_budget: float = Field(gt=0)
    target_weights: dict[str, float] = Field(min_length=1)
    max_position_pct: float = Field(gt=0, le=1)
    max_sector_pct: float = Field(gt=0, le=1)
    min_order_notional: float = Field(gt=0)
    reserve_cash_pct: float = Field(ge=0, lt=1)
    rebalance_band_pct: float = Field(ge=0, lt=1)

    @model_validator(mode="after")
    def validate_target_weights(self) -> PortfolioPlan:
        normalized = {instrument_id.upper(): float(weight) for instrument_id, weight in self.target_weights.items()}
        weight_sum = sum(normalized.values())
        if any(weight <= 0 for weight in normalized.values()):
            raise ConfigurationError("target_weights must contain only positive values.")
        if abs(weight_sum - 1.0) > 1e-6:
            raise ConfigurationError("target_weights must sum to 1.0.")
        self.target_weights = normalized
        return self

    def as_dict(self) -> dict[str, object]:
        return self.model_dump()


def load_portfolio_plan(path: Path | None = None) -> PortfolioPlan:
    settings = get_settings()
    plan_path = path or settings.portfolio_plan_path
    if not plan_path.exists():
        raise ConfigurationError(f"Portfolio plan config not found: {plan_path}")
    payload = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ConfigurationError(f"Portfolio plan config must be a mapping: {plan_path}")
    return PortfolioPlan.model_validate(payload)
