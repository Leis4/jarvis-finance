"""Chronological price-only research with costs and a matched benchmark."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
from scipy.optimize import minimize  # type: ignore[import-untyped]


@dataclass(frozen=True)
class Price:
    day: date
    ticker: str
    close: float
    currency: str


def performance(returns: list[float]) -> dict[str, float | None]:
    values = np.asarray(returns, dtype=float)
    if values.ndim != 1 or not len(values) or not np.all(np.isfinite(values)) or np.any(values <= -1):
        raise ValueError("Finite non-total-loss returns are required")
    equity = np.concatenate(([1.0], np.cumprod(1 + values)))
    sigma = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    return {"total_return": float(equity[-1] - 1),
            "max_drawdown": float(np.min(equity / np.maximum.accumulate(equity) - 1)),
            "sharpe_zero_cash_rate": float(np.mean(values) / sigma * np.sqrt(252)) if sigma > 0 else None}


def walk_forward(prices: Sequence[Price], train_days: int = 60, test_days: int = 20,
                 fee_bps: float = 5.0, slippage_bps: float = 5.0) -> dict[str, Any]:
    """Fit before the execution close and measure only subsequent returns."""
    if train_days < 3 or test_days < 1 or not np.isfinite([fee_bps, slippage_bps]).all() or min(fee_bps, slippage_bps) < 0:
        raise ValueError("Invalid windows or costs")
    if not prices:
        raise ValueError("No prices")
    currencies = {row.currency for row in prices}
    if len(currencies) != 1 or not next(iter(currencies)):
        raise ValueError("One known currency is required; no implicit FX conversion")
    tickers = sorted({row.ticker for row in prices})
    days = sorted({row.day for row in prices})
    if len(tickers) < 2 or len(days) < train_days + 3:
        raise ValueError("Insufficient tickers or chronological history")
    mapping: dict[tuple[date, str], float] = {}
    for row in prices:
        key = (row.day, row.ticker)
        if key in mapping or not np.isfinite(row.close) or row.close <= 0:
            raise ValueError("Duplicate, missing or nonpositive price")
        mapping[key] = row.close
    if len(mapping) != len(tickers) * len(days):
        raise ValueError("Incomplete date grid; no silent forward-fill or dropped dates")
    matrix = np.array([[mapping[day, ticker] for ticker in tickers] for day in days])
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        returns = matrix[1:] / matrix[:-1] - 1
    if not np.all(np.isfinite(returns)) or np.any(returns <= -1):
        raise ValueError("Price changes do not produce finite investable returns")
    count = len(tickers)
    equal = np.ones(count) / count
    actual_weights = np.zeros(count)
    benchmark_weights = np.zeros(count)
    strategy: list[float] = []
    benchmark: list[float] = []
    folds: list[dict[str, Any]] = []
    total_turnover = 0.0
    benchmark_turnover = 0.0
    cost_rate = (fee_bps + slippage_bps) / 10_000
    for start in range(train_days + 1, len(returns), test_days):
        # Return i spans closes i -> i+1. Train stops at close start-1;
        # close start is execution only, never part of model fitting.
        training = returns[start - train_days - 1:start - 1]
        covariance = np.cov(training, rowvar=False)
        if not np.all(np.isfinite(covariance)):
            raise ValueError("Training covariance is not finite")
        result = minimize(lambda w, cov=covariance: float(w @ cov @ w), equal, method="SLSQP",
                          bounds=[(0, max(.5, 1 / count))] * count,
                          constraints=[{"type": "eq", "fun": lambda w: float(np.sum(w) - 1)}],
                          options={"ftol": 1e-12, "maxiter": 200})
        target = np.asarray(result.x, dtype=float)
        upper = max(.5, 1 / count)
        if (not result.success or target.shape != equal.shape or not np.all(np.isfinite(target)) or
                abs(float(target.sum()) - 1) > 1e-6 or np.any(target < -1e-7) or
                np.any(target > upper + 1e-7)):
            raise RuntimeError("Optimizer failed; no substituted success weights")
        turnover = float(np.abs(target - actual_weights).sum())
        baseline_turnover = float(np.abs(equal - benchmark_weights).sum())
        total_turnover += turnover
        benchmark_turnover += baseline_turnover
        actual_weights = target.copy()
        benchmark_weights = equal.copy()
        stop = min(start + test_days, len(returns))
        folds.append({"train_start": days[start - train_days - 1].isoformat(),
                      "train_end": days[start - 1].isoformat(), "execution": days[start].isoformat(),
                      "test_end": days[stop].isoformat(), "test_days": stop - start,
                      "weights": dict(zip(tickers, map(float, target), strict=True)), "turnover": turnover})
        for index in range(start, stop):
            daily = returns[index]
            strategy_gross = float(actual_weights @ daily)
            benchmark_gross = float(benchmark_weights @ daily)
            expense = turnover * cost_rate if index == start else 0.0
            baseline_expense = baseline_turnover * cost_rate if index == start else 0.0
            if max(expense, baseline_expense) >= 1:
                raise ValueError("Costs consume portfolio")
            strategy.append((1 - expense) * (1 + strategy_gross) - 1)
            benchmark.append((1 - baseline_expense) * (1 + benchmark_gross) - 1)
            actual_weights = actual_weights * (1 + daily) / (1 + strategy_gross)
            benchmark_weights = benchmark_weights * (1 + daily) / (1 + benchmark_gross)
    fingerprint = hashlib.sha256(json.dumps([asdict(row) for row in sorted(prices, key=lambda row: (row.day, row.ticker))],
                                            default=str, sort_keys=True).encode()).hexdigest()
    return {"status": "research_only", "production_status": "blocked", "method": "walk_forward_minimum_variance",
            "data_sha256": fingerprint, "currency": next(iter(currencies)), "tickers": tickers,
            "period": [days[0].isoformat(), days[-1].isoformat()], "observations": len(days),
            "train_days": train_days, "test_days": test_days, "fee_bps": fee_bps, "slippage_bps": slippage_bps,
            "risk_free_rate": 0, "evaluated_days": len(strategy), "folds": folds,
            "strategy": performance(strategy), "equal_weight_benchmark": performance(benchmark),
            "turnover": total_turnover, "benchmark_turnover": benchmark_turnover,
            "limitations": ["Unadjusted close: dividends, splits and other corporate actions unverified",
                            "Selected surviving tickers; survivorship bias not assessed",
                            "Fractional positions; lots, taxes and actual broker tariffs unverified",
                            "Cost assumptions are scenarios, not measured execution costs"],
            "daily_strategy_returns": strategy, "daily_benchmark_returns": benchmark}
