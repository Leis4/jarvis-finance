"""Financial metrics for stock scanner."""
from __future__ import annotations

import math
from typing import Any

from jarvis.invest.scanner.errors import ScannerInsufficientData, number


def compute_returns(closes: list[float]) -> list[float]:
    """Daily returns from close prices."""
    out: list[float] = []
    closes = [number(value, positive=True) for value in closes]
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev > 0:
            out.append((closes[i] - prev) / prev)
    return out


def volatility_annual(returns: list[float]) -> float:
    """Annualized volatility (std of returns * sqrt(252))."""
    if len(returns) < 2:
        raise ScannerInsufficientData("At least two returns required")
    returns = [number(value) for value in returns]
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var) * math.sqrt(252)


def sharpe_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    """Research annualized Sharpe against explicit zero reference by default."""
    if len(returns) < 2:
        raise ScannerInsufficientData("At least two returns required")
    returns = [number(value) for value in returns]
    risk_free_rate = number(risk_free_rate)
    mean_daily = sum(returns) / len(returns)
    annual_return = mean_daily * 252
    vol = volatility_annual(returns)
    if vol == 0.0:
        raise ScannerInsufficientData("Sharpe undefined for zero volatility")
    return (annual_return - risk_free_rate) / vol


def max_drawdown(closes: list[float]) -> float:
    """Max peak-to-trough drawdown (negative number or 0)."""
    if not closes:
        raise ScannerInsufficientData("Prices unavailable")
    closes = [number(value, positive=True) for value in closes]
    peak = closes[0]
    max_dd = 0.0
    for price in closes:
        peak = max(peak, price)
        if peak > 0:
            dd = (price - peak) / peak
            max_dd = min(max_dd, dd)
    return max_dd


def total_return(closes: list[float]) -> float:
    """Total return over period."""
    if len(closes) < 2 or closes[0] <= 0:
        raise ScannerInsufficientData("At least two positive prices required")
    closes = [number(value, positive=True) for value in closes]
    return (closes[-1] - closes[0]) / closes[0]


def compute_metrics(candles: list[dict[str, Any]]) -> dict[str, float]:
    """Full metric bundle from candle history."""
    closes: list[float] = []
    for c in candles:
        v = c.get("close") if c.get("close") is not None else c.get("CLOSE")
        closes.append(number(v, positive=True))

    if len(closes) < 20:
        raise ScannerInsufficientData("At least twenty valid closes required")

    returns = compute_returns(closes)
    result = {
        "volatility": volatility_annual(returns),
        "sharpe": sharpe_ratio(returns),
        "max_drawdown": max_drawdown(closes),
        "total_return": total_return(closes),
        "last_price": closes[-1],
        "n_points": float(len(closes)),
    }
    for value in result.values():
        number(value)
    return result
