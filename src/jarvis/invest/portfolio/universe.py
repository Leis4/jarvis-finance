from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, Field, model_validator

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.exceptions import ConfigurationError
from jarvis.invest.portfolio.plan import PortfolioPlan
from jarvis.invest.storage.data_lake import DataLakeStore
from jarvis.invest.storage.market_repository import LocalMarketDataRepository

EXCLUSION_REASON_LABELS = {
    "excluded_by_asset_class": "класс актива не входит в allowed asset classes",
    "excluded_by_currency": "валюта не входит в allowed currencies",
    "excluded_by_board": "режим торгов не входит в allowed boards",
    "excluded_by_ticker": "тикер явно исключён политикой universe",
    "excluded_by_issuer": "эмитент явно исключён политикой universe",
    "excluded_not_tradable": "инструмент нельзя считать торгуемым",
    "excluded_by_history": "недостаточно истории для monthly allocation",
    "excluded_by_quote_staleness": "котировка устарела",
    "excluded_by_liquidity": "ликвидность ниже порога",
    "excluded_by_broker_support": "инструмент не поддерживается брокером",
    "excluded_by_max_candidates": "инструмент не вошёл в top-N кандидатов по score",
    "missing_market_data": "нет рыночных данных по инструменту",
}

POSITIVE_FILTER_LABELS = {
    "asset_class_allowed": "класс актива разрешён policy",
    "currency_allowed": "валюта разрешена policy",
    "board_allowed": "режим торгов разрешён policy",
    "ticker_allowed": "тикер не находится в списке исключений",
    "issuer_allowed": "эмитент не находится в списке исключений",
    "tradable": "есть валидная цена и lot size",
    "history_ok": "история длиннее минимального порога",
    "quote_fresh": "котировка свежая",
    "quote_fresh_not_checked_legacy": "свежесть котировки не проверялась из legacy fallback",
    "liquidity_ok": "ликвидность проходит порог",
    "broker_supported": "инструмент найден в каталоге брокера",
    "broker_support_not_checked": "broker support не проверялся: каталог брокера недоступен",
}


class AllocationUniversePolicy(BaseModel):
    allowed_asset_classes: list[str] = Field(default_factory=lambda: ["EQUITY"])
    allowed_boards: list[str] = Field(default_factory=lambda: ["TQBR"])
    allowed_currencies: list[str] = Field(default_factory=lambda: ["RUB"])
    excluded_tickers: list[str] = Field(default_factory=list)
    excluded_issuers: list[str] = Field(default_factory=list)
    min_liquidity: float = Field(default=5_000_000.0, ge=0.0)
    min_history_length: int = Field(default=60, ge=1)
    quote_staleness_hours: int = Field(default=120, ge=1)
    max_candidates: int = Field(default=25, ge=1)
    require_broker_support: bool = True
    min_strict_broker_catalog_size: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _normalize(self) -> AllocationUniversePolicy:
        self.allowed_asset_classes = [str(item).upper() for item in self.allowed_asset_classes]
        self.allowed_boards = [str(item).upper() for item in self.allowed_boards]
        self.allowed_currencies = [str(item).upper() for item in self.allowed_currencies]
        self.excluded_tickers = [str(item).upper() for item in self.excluded_tickers]
        self.excluded_issuers = [str(item).upper() for item in self.excluded_issuers]
        return self

    def as_dict(self) -> dict[str, object]:
        return self.model_dump()


def load_allocation_universe_policy(path: Path | None = None) -> AllocationUniversePolicy:
    settings = get_settings()
    config_path = path or settings.allocation_universe_policy_path
    if not config_path.exists():
        raise ConfigurationError(f"Allocation universe config not found: {config_path}")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ConfigurationError(f"Allocation universe config must be a mapping: {config_path}")
    return AllocationUniversePolicy.model_validate(payload)


def _safe_timestamp(value: object) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed


def _preferred_universe_frame(asof: str | None = None) -> tuple[pd.DataFrame, str, bool]:
    store = DataLakeStore()
    if store.exists("silver/allocation_universe"):
        frame = store.read_or_empty("silver/allocation_universe").copy()
        bond_frame = store.read_or_empty("silver/bond_universe").copy() if store.exists("silver/bond_universe") else pd.DataFrame()
        if not bond_frame.empty:
            for col, default in (
                ("instrument_id", ""),
                ("ticker", ""),
                ("canonical_instrument_id", ""),
                ("name", ""),
                ("asset_class", "bond"),
                ("currency", "RUB"),
                ("exchange", "MOEX"),
                ("board_id", "TQOB"),
                ("price", 0.0),
                ("close", 0.0),
                ("volume", 0.0),
                ("effective_timestamp", pd.NaT),
                ("source_timestamp", pd.NaT),
                ("ingestion_timestamp", pd.NaT),
                ("feature_asof_timestamp", pd.NaT),
                ("data_version", ""),
                ("lot_size", 1),
                ("license_scope", "public_moex_iss"),
            ):
                if col not in bond_frame.columns:
                    bond_frame[col] = default
            bond_frame["asset_class"] = bond_frame.get("asset_class", "bond").astype(str).str.upper()
            frame = pd.concat([frame, bond_frame], ignore_index=True, sort=False)
        if not frame.empty:
            frame["effective_timestamp"] = pd.to_datetime(frame["effective_timestamp"], utc=True)
            if asof is not None:
                filtered = frame[frame["effective_timestamp"] <= pd.to_datetime(asof, utc=True)]
                if not filtered.empty:
                    return filtered, "silver/allocation_universe", False
            else:
                return frame, "silver/allocation_universe", False
    repo = LocalMarketDataRepository()
    try:
        fallback = repo.read_table("silver", "market").copy()
    except FileNotFoundError:
        return pd.DataFrame(), "missing", True
    if fallback.empty:
        return fallback, "legacy_market_fallback", True
    fallback["effective_timestamp"] = pd.to_datetime(fallback["effective_timestamp"], utc=True)
    if asof is not None:
        fallback = fallback[fallback["effective_timestamp"] <= pd.to_datetime(asof, utc=True)]
    fallback["canonical_instrument_id"] = fallback["instrument_id"].astype(str).str.upper()
    fallback["ticker"] = fallback.get("ticker", fallback["instrument_id"]).astype(str).str.upper()
    fallback["name"] = fallback.get("name", fallback["ticker"])
    fallback["asset_class"] = fallback.get("asset_class", "equity")
    fallback["currency"] = fallback.get("currency", "RUB")
    fallback["exchange"] = fallback.get("exchange", "MOEX")
    fallback["board_id"] = fallback.get("board_id", "TQBR")
    fallback["lot_size"] = fallback.get("lot_size", 1)
    fallback["license_scope"] = fallback.get("license_scope", "local_demo_reference")
    return fallback, "legacy_market_fallback", True


def _preferred_history_frame(asof: str | None = None) -> pd.DataFrame:
    store = DataLakeStore()
    if store.exists("silver/daily_bars"):
        frame = store.read_or_empty("silver/daily_bars").copy()
        if not frame.empty:
            frame["effective_timestamp"] = pd.to_datetime(frame["effective_timestamp"], utc=True)
            if asof is not None:
                filtered = frame[frame["effective_timestamp"] <= pd.to_datetime(asof, utc=True)]
                if not filtered.empty:
                    return filtered
            else:
                return frame
    repo = LocalMarketDataRepository()
    try:
        fallback = repo.read_table("silver", "market").copy()
    except FileNotFoundError:
        return pd.DataFrame()
    if fallback.empty:
        return fallback
    fallback["effective_timestamp"] = pd.to_datetime(fallback["effective_timestamp"], utc=True)
    if asof is not None:
        fallback = fallback[fallback["effective_timestamp"] <= pd.to_datetime(asof, utc=True)]
    fallback["ticker"] = fallback.get("ticker", fallback["instrument_id"]).astype(str).str.upper()
    fallback["board_id"] = fallback.get("board_id", "TQBR")
    fallback["canonical_instrument_id"] = fallback["instrument_id"].astype(str).str.upper()
    fallback["instrument_id"] = fallback["ticker"]
    return fallback


def _as_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return bool(value)


def _history_metrics(history: pd.DataFrame) -> dict[str, float]:
    if history.empty:
        return {
            "history_length": 0.0,
            "clean_history_ratio": 0.0,
            "momentum_20d": 0.0,
            "volatility_20d": 0.0,
            "max_drawdown_120d": -1.0,
            "liquidity_value": 0.0,
        }
    series = history.sort_values("effective_timestamp").copy()
    close = pd.to_numeric(series["close"], errors="coerce")
    volume = pd.to_numeric(series.get("volume", 0.0), errors="coerce").fillna(0.0)
    clean_history_ratio = float(close.notna().mean()) if len(close) else 0.0
    close = close.dropna()
    if close.empty:
        return {
            "history_length": 0.0,
            "clean_history_ratio": 0.0,
            "momentum_20d": 0.0,
            "volatility_20d": 0.0,
            "max_drawdown_120d": -1.0,
            "liquidity_value": 0.0,
        }
    returns = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    rolling_close = close.iloc[-21:] if len(close) >= 21 else close
    momentum_20d = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 and close.iloc[-21] else 0.0
    volatility_20d = float(returns.iloc[-20:].std()) if len(returns) >= 5 else 0.0
    trailing = close.iloc[-120:] if len(close) >= 2 else close
    running_max = trailing.cummax()
    drawdown = trailing / running_max - 1.0
    max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
    traded_value = (rolling_close.reset_index(drop=True) * volume.iloc[-len(rolling_close):].reset_index(drop=True)).dropna()
    liquidity_value = float(traded_value.median()) if not traded_value.empty else 0.0
    return {
        "history_length": float(len(close)),
        "clean_history_ratio": clean_history_ratio,
        "momentum_20d": momentum_20d,
        "volatility_20d": volatility_20d,
        "max_drawdown_120d": max_drawdown,
        "liquidity_value": liquidity_value,
    }


def _score_candidates(frame: pd.DataFrame, policy: AllocationUniversePolicy) -> pd.DataFrame:
    if frame.empty:
        return frame
    scored = frame.copy()
    liquidity_rank = scored["liquidity_value"].rank(method="dense", pct=True)
    momentum_rank = scored["momentum_20d"].rank(method="dense", pct=True)
    history_score = (scored["history_length"] / float(policy.min_history_length)).clip(upper=1.0) * scored["clean_history_ratio"].clip(lower=0.0, upper=1.0)
    volatility_score = (1.0 - ((scored["volatility_20d"] - 0.03).abs() / 0.05)).clip(lower=0.0, upper=1.0)
    drawdown_score = (1.0 - (scored["max_drawdown_120d"].abs() / 0.50)).clip(lower=0.0, upper=1.0)
    scored["score_liquidity"] = liquidity_rank.fillna(0.0)
    scored["score_momentum"] = momentum_rank.fillna(0.0)
    scored["score_history"] = history_score.fillna(0.0)
    scored["score_volatility"] = volatility_score.fillna(0.0)
    scored["score_drawdown"] = drawdown_score.fillna(0.0)
    scored["candidate_score"] = (
        scored["score_momentum"] * 0.30
        + scored["score_liquidity"] * 0.25
        + scored["score_history"] * 0.20
        + scored["score_volatility"] * 0.15
        + scored["score_drawdown"] * 0.10
    ).round(6)
    scored = scored.sort_values(["candidate_score", "momentum_20d", "liquidity_value", "instrument_id"], ascending=[False, False, False, True]).reset_index(drop=True)
    scored["candidate_rank"] = range(1, len(scored) + 1)
    return scored


def _score_explanation(row: pd.Series) -> tuple[list[str], str]:
    reasons = [
        f"ликвидность {float(row.get('liquidity_value', 0.0)):.0f} RUB/день",
        f"история {int(float(row.get('history_length', 0.0)))} дней",
        f"momentum 20d {float(row.get('momentum_20d', 0.0)):.1%}",
        f"volatility 20d {float(row.get('volatility_20d', 0.0)):.1%}",
        f"max drawdown 120d {float(row.get('max_drawdown_120d', 0.0)):.1%}",
    ]
    explanation = (
        f"Score {float(row.get('candidate_score', 0.0)):.3f}: "
        f"ликвидность {float(row.get('score_liquidity', 0.0)):.2f}, "
        f"momentum {float(row.get('score_momentum', 0.0)):.2f}, "
        f"история {float(row.get('score_history', 0.0)):.2f}, "
        f"volatility sanity {float(row.get('score_volatility', 0.0)):.2f}, "
        f"drawdown sanity {float(row.get('score_drawdown', 0.0)):.2f}."
    )
    return reasons, explanation


def _normalize_latest_universe(frame: pd.DataFrame, policy: AllocationUniversePolicy) -> pd.DataFrame:
    if frame.empty:
        return frame
    normalized = frame.copy()
    normalized["canonical_instrument_id"] = normalized.get("canonical_instrument_id", normalized["instrument_id"]).astype(str).str.upper()
    normalized["ticker"] = normalized.get("ticker", normalized["instrument_id"]).astype(str).str.upper()
    normalized["instrument_id"] = normalized["ticker"]
    normalized["board_id"] = normalized["board_id"].map(lambda value: str(value).upper() if pd.notna(value) else None)
    normalized["asset_class"] = normalized["asset_class"].fillna("equity").astype(str).str.upper()
    normalized["currency"] = normalized["currency"].fillna("RUB").astype(str).str.upper()
    normalized["exchange"] = normalized["exchange"].fillna("MOEX").astype(str).str.upper()
    normalized["name"] = normalized["name"].fillna(normalized["ticker"])
    lot_size_series = normalized["lot_size"] if "lot_size" in normalized.columns else pd.Series([1] * len(normalized), index=normalized.index)
    normalized["lot_size"] = pd.to_numeric(lot_size_series, errors="coerce").fillna(1).astype(int)
    normalized["price"] = pd.to_numeric(normalized.get("price", normalized.get("close", 0.0)), errors="coerce").fillna(0.0)
    normalized["close"] = pd.to_numeric(normalized.get("close", normalized["price"]), errors="coerce").fillna(normalized["price"])
    normalized["volume"] = pd.to_numeric(normalized.get("volume", 0.0), errors="coerce").fillna(0.0)
    normalized["sector_name"] = normalized.get("sector_name", "UNKNOWN").fillna("UNKNOWN").astype(str).str.upper()
    normalized["_board_rank"] = normalized["board_id"].map(
        lambda value: policy.allowed_boards.index(value) if value in policy.allowed_boards else len(policy.allowed_boards) + 1
    )
    normalized = normalized.sort_values(
        ["instrument_id", "_board_rank", "effective_timestamp", "volume"],
        ascending=[True, True, False, False],
    )
    normalized = normalized.groupby("instrument_id", as_index=False).head(1).reset_index(drop=True)
    return normalized.drop(columns=["_board_rank"], errors="ignore")


def build_allocation_universe(plan: PortfolioPlan, asof: str | None = None) -> dict[str, object]:
    policy = load_allocation_universe_policy()
    raw_universe, source_name, legacy_fallback = _preferred_universe_frame(asof)
    history = _preferred_history_frame(asof)
    if raw_universe.empty:
        return {
            "source": source_name,
            "policy": policy.as_dict(),
            "raw_universe_size": 0,
            "eligible_universe_size": 0,
            "candidate_count": 0,
            "filtered_out_count": 0,
            "reason_counts": {},
            "top_candidates": [],
            "filtered_out": [],
            "warnings": ["Локальный allocation universe пока пуст. Сначала выполните ingest рынка."],
            "candidate_lookup": {},
            "precomputed_skips": {},
            "allocator_market": pd.DataFrame(),
            "market_snapshot": pd.DataFrame(),
        }
    latest = _normalize_latest_universe(raw_universe, policy)
    if not history.empty:
        history = history.copy()
        history["canonical_instrument_id"] = history.get("canonical_instrument_id", history["instrument_id"]).astype(str).str.upper()
        history["ticker"] = history.get("ticker", history["instrument_id"]).astype(str).str.upper()
        history["board_id"] = history["board_id"].map(lambda value: str(value).upper() if pd.notna(value) else None)
        history["instrument_id"] = history["ticker"]
    market_snapshot = latest.copy()
    now = datetime.now(UTC)
    candidate_rows: list[dict[str, object]] = []
    excluded_rows: list[dict[str, object]] = []
    reason_counts: dict[str, int] = {}

    for _, row in latest.iterrows():
        instrument_id = str(row["instrument_id"]).upper()
        canonical_id = str(row.get("canonical_instrument_id", instrument_id)).upper()
        board_id = str(row.get("board_id")).upper() if pd.notna(row.get("board_id")) else None
        history_subset = history[
            (history["canonical_instrument_id"] == canonical_id)
            | ((history["instrument_id"] == instrument_id) & (history["board_id"] == board_id))
        ] if not history.empty else pd.DataFrame()
        metrics = _history_metrics(history_subset)
        effective_ts = _safe_timestamp(row.get("effective_timestamp"))
        quote_age_hours = (
            (now - effective_ts.to_pydatetime()).total_seconds() / 3600 if effective_ts is not None else None
        )
        issuer_name = str(row.get("issuer_name") or row.get("name") or instrument_id).upper()
        reasons: list[str] = []
        if str(row.get("asset_class", "")).upper() not in policy.allowed_asset_classes:
            reasons.append("excluded_by_asset_class")
        if str(row.get("currency", "")).upper() not in policy.allowed_currencies:
            reasons.append("excluded_by_currency")
        if board_id not in policy.allowed_boards:
            reasons.append("excluded_by_board")
        if instrument_id in policy.excluded_tickers:
            reasons.append("excluded_by_ticker")
        if issuer_name in policy.excluded_issuers:
            reasons.append("excluded_by_issuer")
        if float(row.get("price", 0.0) or 0.0) <= 0 or int(row.get("lot_size", 0) or 0) <= 0:
            reasons.append("excluded_not_tradable")
        if int(metrics["history_length"]) < policy.min_history_length:
            reasons.append("excluded_by_history")
        if not legacy_fallback and quote_age_hours is not None and quote_age_hours > policy.quote_staleness_hours:
            reasons.append("excluded_by_quote_staleness")
        if float(metrics["liquidity_value"]) < policy.min_liquidity:
            reasons.append("excluded_by_liquidity")
        record: dict[str, Any] = {
            "instrument_id": instrument_id,
            "canonical_instrument_id": canonical_id,
            "name": str(row.get("name", instrument_id)),
            "ticker": instrument_id,
            "board_id": board_id,
            "asset_class": str(row.get("asset_class", "")),
            "currency": str(row.get("currency", "")),
            "exchange": str(row.get("exchange", "")),
            "sector_name": str(row.get("sector_name", "UNKNOWN")),
            "price": float(row.get("price", row.get("close", 0.0)) or 0.0),
            "close": float(row.get("close", row.get("price", 0.0)) or 0.0),
            "volume": float(row.get("volume", 0.0) or 0.0),
            "lot_size": int(row.get("lot_size", 1) or 1),
            "effective_timestamp": str(row.get("effective_timestamp")),
            "quote_age_hours": round(float(quote_age_hours), 2) if quote_age_hours is not None else None,
            **metrics,
            "selected_for_plan": instrument_id in plan.target_weights,
        }
        if reasons:
            record["reasons"] = reasons
            excluded_rows.append(record)
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            continue
        candidate_rows.append(record)

    candidates = _score_candidates(pd.DataFrame(candidate_rows), policy)
    if not candidates.empty:
        cutoff = candidates.iloc[: policy.max_candidates].copy()
        if len(candidates) > policy.max_candidates:
            overflow = candidates.iloc[policy.max_candidates :].copy()
            overflow["reasons"] = [["excluded_by_max_candidates"] for _ in range(len(overflow))]
            for _, row in overflow.iterrows():
                record = row.to_dict()
                excluded_rows.append(record)
                reason_counts["excluded_by_max_candidates"] = reason_counts.get("excluded_by_max_candidates", 0) + 1
            candidates = cutoff

        why_included: list[list[str]] = []
        score_explanations: list[str] = []
        why_not_selected: list[str] = []
        for _, row in candidates.iterrows():
            reasons, explanation = _score_explanation(row)
            why_included.append(reasons)
            score_explanations.append(explanation)
            if str(row["instrument_id"]) in plan.target_weights:
                why_not_selected.append("")
            else:
                why_not_selected.append("Инструмент прошёл data/risk filters, но отсутствует в target_weights текущего плана.")
        candidates["why_included"] = why_included
        candidates["score_explanation"] = score_explanations
        candidates["why_not_selected"] = why_not_selected

    candidate_lookup = {
        str(row["instrument_id"]): {
            "candidate_score": float(row.get("candidate_score", 0.0)),
            "candidate_rank": int(row.get("candidate_rank", 0)),
            "why_in_candidates": row.get("score_explanation", ""),
            "candidate_filters_passed": list(row.get("why_included", [])),
            "selected_for_plan": bool(row.get("selected_for_plan", False)),
        }
        for _, row in candidates.iterrows()
    } if not candidates.empty else {}

    exclusion_lookup: dict[str, dict[str, Any]] = {}
    for row in excluded_rows:
        instrument_id = str(row["instrument_id"])
        entry = exclusion_lookup.setdefault(
            instrument_id,
            {
                "instrument_id": instrument_id,
                "target_weight": float(plan.target_weights.get(instrument_id, 0.0)),
                "current_weight": 0.0,
                "deviation_from_target": -float(plan.target_weights.get(instrument_id, 0.0)),
                "sector_name": str(row.get("sector_name", "UNKNOWN")),
                "reasons": [],
                "why_not_selected": "",
            },
        )
        reasons_raw = row.get("reasons", [])
        if isinstance(reasons_raw, (list, tuple)):
            for reason in reasons_raw:
                if reason not in cast(list[Any], entry["reasons"]):
                    cast(list[Any], entry["reasons"]).append(reason)
        entry["why_not_selected"] = "; ".join(cast(list[str], entry["reasons"]))

    for instrument_id in plan.target_weights:
        if instrument_id in candidate_lookup:
            continue
        if instrument_id not in exclusion_lookup:
            exclusion_lookup[instrument_id] = {
                "instrument_id": instrument_id,
                "target_weight": float(plan.target_weights.get(instrument_id, 0.0)),
                "current_weight": 0.0,
                "deviation_from_target": -float(plan.target_weights.get(instrument_id, 0.0)),
                "sector_name": "UNKNOWN",
                "reasons": ["missing_market_data"],
                "why_not_selected": "Инструмент отсутствует в текущем allocation universe.",
            }

    allocator_market = market_snapshot[market_snapshot["instrument_id"].isin(candidate_lookup.keys())].copy() if not market_snapshot.empty else pd.DataFrame()
    warnings: list[str] = []
    if legacy_fallback:
        warnings.append("Используется legacy/demo market fallback.")
    return {
        "source": source_name,
        "policy": policy.as_dict(),
        "raw_universe_size": len(latest),
        "eligible_universe_size": len(candidate_rows),
        "candidate_count": len(candidates),
        "filtered_out_count": len(excluded_rows),
        "reason_counts": reason_counts,
        "top_candidates": candidates.to_dict(orient="records") if not candidates.empty else [],
        "filtered_out": excluded_rows,
        "warnings": warnings,
        "candidate_lookup": candidate_lookup,
        "precomputed_skips": exclusion_lookup,
        "allocator_market": allocator_market,
        "market_snapshot": market_snapshot,
    }
