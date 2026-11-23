"""MOEX bond coupon proxy scanner; YTM/duration require verified cash flows."""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from jarvis.invest.scanner.errors import (
    ScannerInsufficientData,
    ScannerUnavailable,
    fresh_candles,
    number,
)
from jarvis.mcp.adapters.moex import MoexMCP


def compute_bond_metrics(bond: dict[str, Any], candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate annual coupon/clean-price proxy, never YTM or duration."""
    if not candles:
        raise ScannerInsufficientData("Bond price unavailable")
    price = number(candles[-1].get("close", candles[-1].get("CLOSE")), positive=True)
    coupon = number(bond.get("COUPONPERCENT", bond.get("couponpercent")))
    if coupon < 0:
        raise ScannerInsufficientData("Invalid coupon rate")
    # ISS bond prices are percent of nominal; no heuristic RUB conversion.
    annual_coupon_yield = 100 * coupon / price
    years = None
    maturity = bond.get("MATDATE", bond.get("matdate"))
    if maturity:
        try:
            days = (date.fromisoformat(str(maturity)[:10]) - datetime.now(UTC).date()).days
        except ValueError:
            raise ScannerInsufficientData("Invalid maturity date") from None
        if days <= 0:
            raise ScannerInsufficientData("Matured instrument")
        years = days / 365.25
    volumes = [c.get("volume", c.get("VOLUME")) for c in candles]
    volume = None if any(v is None for v in volumes) else sum(number(v) for v in volumes)
    if volume is not None and volume < 0:
        raise ScannerInsufficientData("Negative volume")
    return {"annual_coupon_yield_pct": annual_coupon_yield, "ytm": None,
            "duration_years": None, "years_to_maturity": years,
            "last_price": price, "price_unit": "percent_of_nominal",
            "coupon_rate": coupon, "volume": volume, "status": "research_only"}


def scan_bonds(moex: MoexMCP, top_n: int = 10, max_candidates: int = 50,
               min_ytm: float | None = None, *, min_annual_coupon_yield: float = 0.0) -> list[dict[str, Any]]:
    """Rank coupon proxies; unavailable YTM cannot be used as a filter."""
    if top_n <= 0 or max_candidates <= 0 or min_ytm is not None:
        raise ScannerInsufficientData("Positive limits required; YTM filter unavailable")
    threshold = number(min_annual_coupon_yield)
    try:
        bonds = moex.get_all_bonds(limit=max_candidates)
    except Exception:  # noqa: BLE001 - external source boundary
        raise ScannerUnavailable("Bond universe source unavailable") from None
    if not bonds:
        raise ScannerInsufficientData("Bond universe unavailable")
    candidates = []
    for bond in bonds[:max_candidates]:
        ticker = str(bond.get("secid") or bond.get("SECID") or "")
        if not ticker:
            raise ScannerInsufficientData("Instrument identity unavailable")
        try:
            candles = moex.get_candles(ticker, interval=24, days=30, market="bonds")
        except Exception:  # noqa: BLE001 - external source boundary
            raise ScannerUnavailable("Bond candle source unavailable") from None
        observed = fresh_candles(candles)
        metrics = compute_bond_metrics(bond, candles)
        if metrics["annual_coupon_yield_pct"] < threshold:
            continue
        candidates.append({"ticker": ticker, "name": str(bond.get("shortname") or bond.get("SHORTNAME") or ticker)[:40],
                           **metrics, "observed_at": observed, "source": "MOEX ISS bonds"})
    candidates.sort(key=lambda b: -b["annual_coupon_yield_pct"])
    return candidates[:top_n]
