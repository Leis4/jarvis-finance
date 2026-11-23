from __future__ import annotations

from typing import Any

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.core.utils import stable_hash


def _normalize_records(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    for column in (
        "source_name",
        "source_instrument_id",
        "ticker",
        "board_id",
        "exchange",
        "isin",
        "figi",
        "uid",
        "asset_class",
        "currency",
        "license_scope",
        "name",
        "sector_name",
    ):
        if column not in normalized.columns:
            normalized[column] = None
        normalized[column] = normalized[column].where(normalized[column].notna(), None)
        normalized[column] = normalized[column].map(lambda value: str(value).upper() if isinstance(value, str) else value)
    if "instrument_id" not in normalized.columns:
        normalized["instrument_id"] = normalized["source_instrument_id"]
    return normalized


def _mapping_key(row: pd.Series) -> str:
    isin = row.get("isin")
    figi = row.get("figi")
    uid = row.get("uid")
    ticker = row.get("ticker")
    board_id = row.get("board_id")
    exchange = row.get("exchange") or "UNKNOWN"
    if isinstance(isin, str) and isin:
        return f"ISIN:{isin}"
    if isinstance(figi, str) and figi:
        return f"FIGI:{figi}"
    if isinstance(uid, str) and uid:
        return f"UID:{uid}"
    if isinstance(ticker, str) and ticker and isinstance(board_id, str) and board_id:
        return f"{exchange}:{board_id}:{ticker}"
    if isinstance(ticker, str) and ticker:
        return f"{exchange}:TICKER:{ticker}"
    source_instrument_id = row.get("source_instrument_id") or row.get("instrument_id") or "UNKNOWN"
    return f"SYNTH:{stable_hash({'source': row.get('source_name'), 'id': source_instrument_id})[:12]}"


def _canonical_id(group: pd.DataFrame, duplicated_tickers: set[str]) -> tuple[str, str]:
    preferred = group.iloc[0]
    ticker = preferred.get("ticker")
    board_id = preferred.get("board_id")
    isin = preferred.get("isin")
    figi = preferred.get("figi")
    uid = preferred.get("uid")
    if isinstance(ticker, str) and ticker:
        if ticker not in duplicated_tickers:
            return ticker, "ticker_unique"
        if isinstance(board_id, str) and board_id:
            return f"{ticker}@{board_id}", "ticker_board_disambiguated"
    if isinstance(isin, str) and isin:
        return isin, "isin_fallback"
    if isinstance(figi, str) and figi:
        return f"FIGI:{figi}", "figi_fallback"
    if isinstance(uid, str) and uid:
        return f"UID:{uid}", "uid_fallback"
    source_instrument_id = preferred.get("source_instrument_id") or preferred.get("instrument_id") or "UNKNOWN"
    digest = stable_hash({"mapping_key": preferred.get("mapping_key"), "source_instrument_id": source_instrument_id})[:12]
    return f"INSTR:{digest}", "synthetic_hash"


def build_instrument_mapping(frames: list[pd.DataFrame]) -> pd.DataFrame:
    non_empty = [frame for frame in frames if not frame.empty]
    if not non_empty:
        return pd.DataFrame(
            columns=[
                "instrument_id",
                "canonical_instrument_id",
                "source_name",
                "source_instrument_id",
                "mapping_key",
                "conflict_resolution",
                "ticker",
                "board_id",
                "exchange",
                "isin",
                "figi",
                "uid",
                "asset_class",
                "currency",
                "name",
                "sector_name",
                "license_scope",
            ]
        )
    combined = pd.concat([_normalize_records(frame) for frame in non_empty], ignore_index=True)
    combined["mapping_key"] = combined.apply(_mapping_key, axis=1)
    tickers = combined["ticker"].dropna().astype(str)
    duplicated_tickers = set(tickers[tickers.duplicated(keep=False)].tolist())
    mapping_rows: list[dict[str, Any]] = []
    for _, group in combined.groupby("mapping_key", dropna=False):
        canonical_id, resolution = _canonical_id(group.reset_index(drop=True), duplicated_tickers)
        for _, row in group.iterrows():
            mapping_rows.append(
                {
                    "instrument_id": canonical_id,
                    "canonical_instrument_id": canonical_id,
                    "source_name": row.get("source_name"),
                    "source_instrument_id": row.get("source_instrument_id") or row.get("instrument_id"),
                    "mapping_key": row.get("mapping_key"),
                    "conflict_resolution": resolution,
                    "ticker": row.get("ticker"),
                    "board_id": row.get("board_id"),
                    "exchange": row.get("exchange"),
                    "isin": row.get("isin"),
                    "figi": row.get("figi"),
                    "uid": row.get("uid"),
                    "asset_class": row.get("asset_class"),
                    "currency": row.get("currency"),
                    "name": row.get("name"),
                    "sector_name": row.get("sector_name"),
                    "license_scope": row.get("license_scope"),
                }
            )
    mapping = pd.DataFrame(mapping_rows).drop_duplicates(subset=["source_name", "source_instrument_id"], keep="last")
    return mapping.sort_values(["canonical_instrument_id", "source_name", "source_instrument_id"]).reset_index(drop=True)


def build_instrument_master(mapping: pd.DataFrame) -> pd.DataFrame:
    if mapping.empty:
        return pd.DataFrame(
            columns=[
                "instrument_id",
                "canonical_instrument_id",
                "name",
                "ticker",
                "board_id",
                "exchange",
                "isin",
                "figi",
                "uid",
                "asset_class",
                "currency",
                "sector_name",
                "license_scope",
            ]
        )
    source_priority = {"MOEX_ISS": 0, "TBANK": 1, "CBONDS": 2, "SAMPLE_DATA": 3}
    ranked = mapping.copy()
    ranked["source_priority"] = ranked["source_name"].map(
        lambda value: source_priority.get(str(value).upper(), 99)
    )
    rows: list[dict[str, Any]] = []
    for canonical_id, group in ranked.sort_values("source_priority").groupby("canonical_instrument_id"):
        base = group.iloc[0].to_dict()
        row = {
            "instrument_id": canonical_id,
            "canonical_instrument_id": canonical_id,
            "name": next((value for value in group["name"] if isinstance(value, str) and value), base.get("name")),
            "ticker": next((value for value in group["ticker"] if isinstance(value, str) and value), base.get("ticker")),
            "board_id": next((value for value in group["board_id"] if isinstance(value, str) and value), base.get("board_id")),
            "exchange": next((value for value in group["exchange"] if isinstance(value, str) and value), base.get("exchange")),
            "isin": next((value for value in group["isin"] if isinstance(value, str) and value), base.get("isin")),
            "figi": next((value for value in group["figi"] if isinstance(value, str) and value), base.get("figi")),
            "uid": next((value for value in group["uid"] if isinstance(value, str) and value), base.get("uid")),
            "asset_class": next((value for value in group["asset_class"] if isinstance(value, str) and value), base.get("asset_class")),
            "currency": next((value for value in group["currency"] if isinstance(value, str) and value), base.get("currency")),
            "sector_name": next((value for value in group["sector_name"] if isinstance(value, str) and value), "UNKNOWN"),
            "license_scope": next((value for value in group["license_scope"] if isinstance(value, str) and value), base.get("license_scope")),
        }
        rows.append(row)
    return pd.DataFrame(rows).sort_values("instrument_id").reset_index(drop=True)
