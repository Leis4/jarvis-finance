"""Advanced risk metrics: VaR, CVaR, Monte Carlo, and portfolio metrics."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray


class RiskInputError(ValueError):
    """Risk inputs violate the finite, aligned numerical contract."""


class RiskInsufficientData(RiskInputError):
    """A risk estimate cannot be supported by the supplied sample."""


def _vector(values: list[float]) -> NDArray[np.float64]:
    try:
        arr = np.asarray(values, dtype=np.float64)
    except (ValueError, TypeError):
        raise RiskInputError("Risk inputs must be numeric") from None
    if arr.ndim != 1 or not np.all(np.isfinite(arr)):
        raise RiskInputError("Risk inputs must be finite vectors")
    return arr


def _sample(returns: list[float], confidence: float) -> NDArray[np.float64]:
    if not np.isfinite(confidence) or not 0 < confidence < 1:
        raise RiskInputError("Confidence must be between zero and one")
    arr = _vector(returns)
    if len(arr) < 2:
        raise RiskInsufficientData("At least two observed returns required")
    return arr


def compute_var(returns: list[float], confidence: float = 0.95) -> float:
    """Return the signed lower empirical return quantile; positive means a gain."""
    arr = _sample(returns, confidence)
    alpha = 1.0 - confidence
    val = float(np.percentile(arr, alpha * 100))
    if not np.isfinite(val):
        raise RiskInputError("Return quantile overflowed")
    return val


def compute_cvar(returns: list[float], confidence: float = 0.95) -> float:
    """Return the mean observed return below the signed lower quantile."""
    arr = _sample(returns, confidence)
    var = compute_var(returns, confidence=confidence)
    tail = arr[arr <= var]
    cvar_val = float(np.mean(tail))
    if not np.isfinite(cvar_val):
        raise RiskInputError("Tail mean overflowed")
    return cvar_val



def monte_carlo_portfolio(
    weights: list[float],
    means: list[float],
    cov: list[list[float]],
    n_sims: int = 100000,
    horizon_days: int = 30,
) -> dict[str, float]:
    """Simulate portfolio returns using Monte Carlo Brownian Motion model.

    Formula:
      port_mean = sum(w_i * mu_i) * horizon_days
      port_vol = sqrt(w.T @ Sigma @ w) * sqrt(horizon_days)
      sim_returns ~ N(port_mean, port_vol)
    """
    if not weights or not means or not cov:
        raise RiskInsufficientData("Portfolio moments unavailable")
    if (type(n_sims) is not int or not 2 <= n_sims <= 1_000_000 or
            type(horizon_days) is not int or horizon_days <= 0):
        raise RiskInputError("Positive horizon and two to one million simulations required")
    w = _vector(weights)
    m = _vector(means)
    try:
        c = np.asarray(cov, dtype=float)
    except (TypeError, ValueError):
        raise RiskInputError("Covariance must be numeric") from None
    if (w.shape != m.shape or c.shape != (len(w), len(w)) or
            not np.all(np.isfinite(c)) or np.any(w < 0) or
            not np.isclose(w.sum(), 1, rtol=0, atol=1e-10)):
        raise RiskInputError("Aligned finite long-only weights and covariance required")
    if not np.allclose(c, c.T, rtol=1e-10, atol=1e-12):
        raise RiskInputError("Covariance must be symmetric")
    try:
        eigenvalues = np.linalg.eigvalsh(c)
        rank = np.linalg.matrix_rank(c)
    except np.linalg.LinAlgError:
        raise RiskInputError("Covariance decomposition failed") from None
    if np.min(eigenvalues) < 0:
        raise RiskInputError("Covariance must be positive semidefinite")
    if rank < len(w):
        raise RiskInsufficientData("Singular covariance does not support this risk model")

    try:
        port_mean = float(np.dot(w, m)) * horizon_days
        port_vol = float(np.sqrt(np.dot(w.T, np.dot(c, w)))) * np.sqrt(horizon_days)
    except OverflowError:
        raise RiskInputError("Portfolio horizon overflowed") from None

    if not np.isfinite(port_mean) or not np.isfinite(port_vol):
        raise RiskInputError("Portfolio moments overflowed")
    rng = np.random.default_rng(42)
    sim_returns = rng.normal(port_mean, port_vol, n_sims)

    p5 = float(np.percentile(sim_returns, 5))
    p50 = float(np.percentile(sim_returns, 50))
    p95 = float(np.percentile(sim_returns, 95))
    expected = float(np.mean(sim_returns))

    return {
        "p5": round(p5, 6),
        "p50": round(p50, 6),
        "p95": round(p95, 6),
        "expected": round(expected, 6),
    }


def portfolio_metrics(prices: dict[str, list[float]], *,
                      observation_dates: dict[str, list[str]] | None = None,
                      as_of: datetime | None = None, max_age_days: int = 7) -> dict[str, Any]:
    """Compute price-only equal-weight research risk from aligned price histories."""
    if not prices:
        raise RiskInsufficientData("Price history unavailable")
    current = as_of or datetime.now(UTC)
    if current.tzinfo is None or type(max_age_days) is not int or max_age_days < 0:
        raise RiskInputError("Aware as-of time and nonnegative freshness age required")
    freshness = "unverified"
    period: list[str] | None = None
    if observation_dates is not None:
        if set(observation_dates) != set(prices):
            raise RiskInputError("Every instrument needs dated observations")
        common: list[date] | None = None
        for ticker, values in observation_dates.items():
            try:
                grid = [date.fromisoformat(value) for value in values]
            except (ValueError, TypeError):
                raise RiskInputError("Invalid observation date") from None
            if (len(grid) != len(prices[ticker]) or not grid or
                    grid != sorted(set(grid)) or (common is not None and grid != common)):
                raise RiskInputError("Identical increasing observation grids required")
            if grid[-1] > current.astimezone(UTC).date():
                raise RiskInputError("Future observation date")
            if (current.astimezone(UTC).date() - grid[-1]).days > max_age_days:
                raise RiskInsufficientData("Price observations are stale")
            common = grid
        if common is not None:
            period = [common[0].isoformat(), common[-1].isoformat()]
        freshness = "available"

    returns_list: list[list[float]] = []
    tickers: list[str] = []

    for ticker, series in prices.items():
        if len(series) < 3:
            raise RiskInsufficientData("Every instrument needs at least three prices")
        arr = _vector(series)
        if np.any(arr <= 0):
            raise RiskInputError("Prices must be strictly positive")
        ret = np.diff(arr) / arr[:-1]
        returns_list.append(ret.tolist())
        tickers.append(ticker)

    if len({len(r) for r in returns_list}) != 1:
        raise RiskInputError("Price histories must have matching observation grids")
    ret_matrix = np.array(returns_list)
    n_assets = len(tickers)
    weights = [1.0 / n_assets] * n_assets

    combined_returns = np.dot(np.array(weights), ret_matrix)

    var = compute_var(combined_returns.tolist(), confidence=0.95)
    cvar = compute_cvar(combined_returns.tolist(), confidence=0.95)

    means = np.mean(ret_matrix, axis=1).tolist()
    cov = np.cov(ret_matrix).tolist() if n_assets > 1 else [[float(np.var(ret_matrix))]]

    mc = monte_carlo_portfolio(weights, means, cov, horizon_days=30)

    return {
        "status": "research_only",
        "return_basis": "price_only",
        "freshness": freshness,
        "period": period,
        "calculated_at": current.astimezone(UTC).isoformat(),
        "source": "caller_snapshot",
        "data_sha256": hashlib.sha256(json.dumps(
            {"prices": prices, "dates": observation_dates}, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest(),
        "units": "fractional_simple_returns",
        "assumptions": "Equal weights; date alignment must be supplied by caller; normal IID daily returns",
        "var_95": round(var, 4),
        "cvar_95": round(cvar, 4),
        "mc_30d": mc,
    }
