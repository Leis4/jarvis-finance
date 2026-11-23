"""Market scanner — ranking MOEX shares by metrics."""
from __future__ import annotations

from dataclasses import dataclass

from jarvis.invest.scanner.errors import (
    ScannerInsufficientData,
    ScannerUnavailable,
    fresh_candles,
    number,
)
from jarvis.invest.scanner.metrics import compute_metrics
from jarvis.mcp.adapters.moex import MoexMCP


@dataclass
class StockCandidate:
    ticker: str
    name: str
    last_price: float
    total_return: float
    volatility: float
    sharpe: float
    max_drawdown: float
    score: float


def scan_stocks(
    moex: MoexMCP,
    top_n: int = 10,
    max_candidates: int = 50,
    min_volatility: float = 0.0,
    max_volatility: float = 0.50,
    min_sharpe: float = 0.0,
) -> list[StockCandidate]:
    """Scan MOEX shares, return top N by score."""
    if top_n <= 0 or max_candidates <= 0:
        raise ScannerInsufficientData("Positive scan limits required")
    if number(min_volatility) < 0 or number(max_volatility) < min_volatility:
        raise ScannerInsufficientData("Invalid volatility bounds")
    number(min_sharpe)
    try:
        shares = moex.get_all_shares(limit=max_candidates)
    except Exception:  # noqa: BLE001 - external source boundary
        raise ScannerUnavailable("Stock universe source unavailable") from None
    if not shares:
        raise ScannerInsufficientData("Stock universe unavailable")

    candidates: list[StockCandidate] = []
    for share in shares[:max_candidates]:
        ticker = str(share.get("secid") or share.get("SECID") or "")
        if not ticker:
            raise ScannerInsufficientData("Instrument identity unavailable")
        name = str(share.get("shortname") or share.get("name") or ticker)

        try:
            candles = moex.get_candles(ticker, interval=24, days=365)
        except Exception:  # noqa: BLE001 - external source boundary
            raise ScannerUnavailable("Stock candle source unavailable") from None
        if not candles or len(candles) < 60:
            raise ScannerInsufficientData("Stock history too short")
        fresh_candles(candles)

        m = compute_metrics(candles)
        if m["volatility"] < min_volatility or m["volatility"] > max_volatility:
            continue
        if m["sharpe"] < min_sharpe:
            continue

        score = 0.5 * m["sharpe"] + 0.3 * m["total_return"] - 0.2 * abs(m["max_drawdown"])

        candidates.append(
            StockCandidate(
                ticker=ticker,
                name=name[:40],
                last_price=m["last_price"],
                total_return=m["total_return"],
                volatility=m["volatility"],
                sharpe=m["sharpe"],
                max_drawdown=m["max_drawdown"],
                score=score,
            )
        )

    candidates.sort(key=lambda c: -c.score)
    return candidates[:top_n]
