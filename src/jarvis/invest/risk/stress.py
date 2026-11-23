"""Portfolio stress testing scenarios."""
from __future__ import annotations

from typing import Any

VALID_SCENARIOS = {"2008_crisis", "covid_2020", "rate_up_5pct", "ruble_devaluation"}


def stress_test_scenario(portfolio: dict[str, float], scenario: str) -> dict[str, Any]:
    """Calculate portfolio loss under historical/macro stress scenarios.

    portfolio: dict of ticker -> total current value (RUB)
    scenario: '2008_crisis', 'covid_2020', 'rate_up_5pct', 'ruble_devaluation'
    """
    if scenario not in VALID_SCENARIOS:
        raise ValueError(f"Unknown scenario: {scenario}. Valid: {sorted(VALID_SCENARIOS)}")

    if not portfolio:
        return {
            "scenario": scenario,
            "portfolio_loss": 0.0,
            "portfolio_loss_pct": 0.0,
            "worst_asset": "NONE",
            "recovery_estimate_days": 0,
            "asset_impacts": {},
        }

    total_val = sum(portfolio.values())
    if total_val <= 0:
        return {
            "scenario": scenario,
            "portfolio_loss": 0.0,
            "portfolio_loss_pct": 0.0,
            "worst_asset": "NONE",
            "recovery_estimate_days": 0,
            "asset_impacts": {},
        }

    impacts: dict[str, float] = {}

    for ticker, val in portfolio.items():
        t_upper = ticker.upper()

        if scenario == "2008_crisis":
            if "OFZ" in t_upper or "BOND" in t_upper:
                change = -0.10
            elif "GOLD" in t_upper or "GLDRUB" in t_upper:
                change = 0.20
            else:
                change = -0.50

        elif scenario == "covid_2020":
            if "OFZ" in t_upper or "BOND" in t_upper:
                change = -0.05
            elif "GOLD" in t_upper or "GLDRUB" in t_upper:
                change = 0.10
            else:
                change = -0.30

        elif scenario == "rate_up_5pct":
            if "OFZ" in t_upper or "BOND" in t_upper:
                duration = 5.0
                change = -0.05 * duration  # -25%
            elif "CASH" in t_upper or "RUB" in t_upper:
                change = 0.0
            else:
                change = -0.10

        elif scenario == "ruble_devaluation":
            if any(exp in t_upper for exp in ["LKOH", "ROSN", "NVTK", "GAZP", "GMKN", "ALRS", "CHMF", "NLMK", "PLZL"]):
                change = 0.15
            elif any(imp in t_upper for imp in ["MVID", "OZON", "MGNT", "FIXP"]):
                change = -0.20
            else:
                change = -0.05

        else:
            change = 0.0

        impacts[ticker] = val * change

    total_loss = sum(impacts.values())
    total_loss_pct = total_loss / total_val

    worst_asset = min(impacts.items(), key=lambda x: x[1])[0]

    recovery_days = {
        "2008_crisis": 365,
        "covid_2020": 180,
        "rate_up_5pct": 120,
        "ruble_devaluation": 90,
    }.get(scenario, 180)

    return {
        "scenario": scenario,
        "portfolio_loss": round(total_loss, 2),
        "portfolio_loss_pct": round(total_loss_pct, 4),
        "worst_asset": worst_asset,
        "recovery_estimate_days": recovery_days,
        "asset_impacts": {k: round(v, 2) for k, v in impacts.items()},
    }
