from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.core.exceptions import DataValidationError

COMMON_COLUMNS = (
    "instrument_id",
    "source_name",
    "source_timestamp",
    "ingestion_timestamp",
    "effective_timestamp",
    "feature_asof_timestamp",
    "data_version",
)

COMMON_OPTIONAL_COLUMNS = (
    "asset_class",
    "currency",
    "exchange",
    "board_id",
    "ticker",
    "isin",
    "figi",
    "uid",
    "lot_size",
    "price",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "accrued_interest",
    "duration",
    "yield",
    "is_delayed",
    "license_scope",
)


@dataclass(slots=True)
class DatasetSchema:
    key: str
    required_columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    default_values: dict[str, Any] = field(default_factory=dict)
    timestamp_columns: tuple[str, ...] = (
        "source_timestamp",
        "ingestion_timestamp",
        "effective_timestamp",
        "feature_asof_timestamp",
    )

    def ordered_columns(self, frame: pd.DataFrame) -> list[str]:
        ordered = list(COMMON_COLUMNS)
        for column in self.required_columns:
            if column not in ordered:
                ordered.append(column)
        for column in COMMON_OPTIONAL_COLUMNS:
            if column in frame.columns and column not in ordered:
                ordered.append(column)
        for column in frame.columns:
            if column not in ordered:
                ordered.append(column)
        return ordered


SCHEMA_REGISTRY: dict[str, DatasetSchema] = {
    "raw/tbank/accounts": DatasetSchema(
        key="raw/tbank/accounts",
        required_columns=(
            "account_id",
            "account_name",
            "account_type",
            "broker_mode",
            "currency",
            "source_activation",
            "license_scope",
        ),
        primary_key=("account_id", "effective_timestamp"),
        default_values={"asset_class": "account", "exchange": "T_BANK"},
    ),
    "raw/tbank/positions": DatasetSchema(
        key="raw/tbank/positions",
        required_columns=(
            "account_id",
            "quantity",
            "average_price",
            "market_value",
            "source_activation",
            "license_scope",
        ),
        primary_key=("account_id", "instrument_id", "effective_timestamp"),
        default_values={"asset_class": "security", "exchange": "T_BANK"},
    ),
    "raw/tbank/operations": DatasetSchema(
        key="raw/tbank/operations",
        required_columns=(
            "account_id",
            "operation_id",
            "operation_type",
            "status",
            "payment",
            "source_activation",
            "license_scope",
        ),
        primary_key=("operation_id",),
        default_values={"exchange": "T_BANK"},
    ),
    "raw/tbank/orders": DatasetSchema(
        key="raw/tbank/orders",
        required_columns=(
            "account_id",
            "order_id",
            "side",
            "quantity",
            "status",
            "source_activation",
            "license_scope",
        ),
        primary_key=("order_id",),
        default_values={"exchange": "T_BANK"},
    ),
    "raw/tbank/fills": DatasetSchema(
        key="raw/tbank/fills",
        required_columns=(
            "account_id",
            "fill_id",
            "order_id",
            "side",
            "quantity",
            "status",
            "source_activation",
            "license_scope",
        ),
        primary_key=("fill_id",),
        default_values={"exchange": "T_BANK"},
    ),
    "raw/tbank/quotes_watchlist": DatasetSchema(
        key="raw/tbank/quotes_watchlist",
        required_columns=("source_activation", "license_scope"),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"exchange": "T_BANK", "asset_class": "security"},
    ),
    "raw/tbank/instruments": DatasetSchema(
        key="raw/tbank/instruments",
        required_columns=("name", "source_instrument_id", "source_activation", "license_scope"),
        primary_key=("source_name", "source_instrument_id"),
        default_values={"exchange": "T_BANK", "asset_class": "security"},
    ),
    "raw/moex/securities": DatasetSchema(
        key="raw/moex/securities",
        required_columns=("name", "source_instrument_id", "license_scope"),
        primary_key=("board_id", "ticker"),
        default_values={"exchange": "MOEX", "source_name": "moex_iss", "is_delayed": True},
    ),
    "raw/moex/boards": DatasetSchema(
        key="raw/moex/boards",
        required_columns=("board_title", "market", "engine", "license_scope"),
        primary_key=("board_id",),
        default_values={"exchange": "MOEX", "asset_class": "board", "source_name": "moex_iss"},
    ),
    "raw/moex/candles_daily": DatasetSchema(
        key="raw/moex/candles_daily",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"exchange": "MOEX", "source_name": "moex_iss", "is_delayed": True},
    ),
    "raw/moex/candles_intraday": DatasetSchema(
        key="raw/moex/candles_intraday",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"exchange": "MOEX", "source_name": "moex_iss", "is_delayed": True},
    ),
    "raw/moex/quotes": DatasetSchema(
        key="raw/moex/quotes",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"exchange": "MOEX", "source_name": "moex_iss", "is_delayed": True},
    ),
    "raw/moex/trades": DatasetSchema(
        key="raw/moex/trades",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"exchange": "MOEX", "source_name": "moex_iss", "is_delayed": True},
    ),
    "raw/cbonds/emissions": DatasetSchema(
        key="raw/cbonds/emissions",
        required_columns=("source_instrument_id", "license_scope"),
        primary_key=("source_name", "source_instrument_id"),
        default_values={"exchange": "CBONDS", "source_name": "cbonds", "asset_class": "bond"},
    ),
    "raw/cbonds/quotes_eod": DatasetSchema(
        key="raw/cbonds/quotes_eod",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"exchange": "CBONDS", "source_name": "cbonds", "asset_class": "bond"},
    ),
    "raw/cbonds/analytics": DatasetSchema(
        key="raw/cbonds/analytics",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"exchange": "CBONDS", "source_name": "cbonds", "asset_class": "bond"},
    ),
    "bronze/instruments": DatasetSchema(
        key="bronze/instruments",
        required_columns=("canonical_instrument_id", "name", "source_instrument_id", "license_scope"),
        primary_key=("source_name", "source_instrument_id"),
    ),
    "bronze/market_bars": DatasetSchema(
        key="bronze/market_bars",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
    ),
    "bronze/quotes": DatasetSchema(
        key="bronze/quotes",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
    ),
    "bronze/portfolio": DatasetSchema(
        key="bronze/portfolio",
        required_columns=("account_id", "cash_balance", "portfolio_value", "license_scope"),
        primary_key=("account_id", "effective_timestamp"),
        default_values={"asset_class": "portfolio"},
    ),
    "bronze/operations": DatasetSchema(
        key="bronze/operations",
        required_columns=("operation_id", "account_id", "status", "license_scope"),
        primary_key=("operation_id",),
    ),
    "silver/instrument_master": DatasetSchema(
        key="silver/instrument_master",
        required_columns=("canonical_instrument_id", "name", "license_scope"),
        primary_key=("instrument_id",),
    ),
    "silver/instrument_mapping": DatasetSchema(
        key="silver/instrument_mapping",
        required_columns=(
            "canonical_instrument_id",
            "source_instrument_id",
            "mapping_key",
            "conflict_resolution",
            "license_scope",
        ),
        primary_key=("source_name", "source_instrument_id"),
    ),
    "silver/daily_bars": DatasetSchema(
        key="silver/daily_bars",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
    ),
    "silver/market": DatasetSchema(
        key="silver/market",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
    ),
    "silver/benchmark": DatasetSchema(
        key="silver/benchmark",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"asset_class": "index"},
    ),
    "silver/sector": DatasetSchema(
        key="silver/sector",
        required_columns=("sector_name", "license_scope"),
        primary_key=("sector_name", "effective_timestamp"),
        default_values={"asset_class": "sector"},
    ),
    "silver/fundamentals": DatasetSchema(
        key="silver/fundamentals",
        required_columns=("market_cap", "pe_ratio", "license_scope"),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"asset_class": "fundamental"},
    ),
    "silver/sentiment": DatasetSchema(
        key="silver/sentiment",
        required_columns=("sentiment_score", "event_flag_news", "license_scope"),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"asset_class": "news_sentiment"},
    ),
    "silver/corporate_actions": DatasetSchema(
        key="silver/corporate_actions",
        required_columns=("event_flag_corporate", "license_scope"),
        primary_key=("instrument_id", "effective_timestamp"),
        default_values={"asset_class": "corporate_action"},
    ),
    "silver/news_events": DatasetSchema(
        key="silver/news_events",
        required_columns=("event_id", "event_type", "headline", "license_scope"),
        primary_key=("event_id",),
        default_values={"asset_class": "news_event"},
    ),
    "silver/latest_quotes": DatasetSchema(
        key="silver/latest_quotes",
        required_columns=("license_scope",),
        primary_key=("instrument_id",),
    ),
    "silver/portfolio_snapshots": DatasetSchema(
        key="silver/portfolio_snapshots",
        required_columns=("account_id", "cash_balance", "portfolio_value", "license_scope"),
        primary_key=("account_id", "effective_timestamp"),
        default_values={"asset_class": "portfolio"},
    ),
    "silver/portfolio_current": DatasetSchema(
        key="silver/portfolio_current",
        required_columns=("account_id", "cash_balance", "portfolio_value", "license_scope"),
        primary_key=("account_id",),
        default_values={"asset_class": "portfolio"},
    ),
    "silver/allocation_universe": DatasetSchema(
        key="silver/allocation_universe",
        required_columns=("name", "license_scope"),
        primary_key=("instrument_id",),
    ),
    "silver/bond_universe": DatasetSchema(
        key="silver/bond_universe",
        required_columns=("name", "license_scope"),
        primary_key=("instrument_id",),
        default_values={"asset_class": "bond"},
    ),
    "silver/operations_ledger": DatasetSchema(
        key="silver/operations_ledger",
        required_columns=("operation_id", "account_id", "status", "license_scope"),
        primary_key=("operation_id",),
    ),
    "silver/monthly_market_snapshot": DatasetSchema(
        key="silver/monthly_market_snapshot",
        required_columns=("license_scope", "snapshot_month"),
        primary_key=("instrument_id", "snapshot_month"),
    ),
    "silver/model_predictions": DatasetSchema(
        key="silver/model_predictions",
        required_columns=(
            "model_name",
            "model_family",
            "model_version",
            "target_name",
            "score",
            "probability",
            "confidence",
            "rank",
            "training_run_id",
            "artifact_id",
        ),
        primary_key=("training_run_id", "instrument_id"),
        default_values={"license_scope": "local_model_runtime"},
    ),
    "features/allocation_features": DatasetSchema(
        key="features/allocation_features",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
    ),
    "features/allocation_features_v2": DatasetSchema(
        key="features/allocation_features_v2",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
    ),
    "features/market_features_v2": DatasetSchema(
        key="features/market_features_v2",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "effective_timestamp"),
    ),
    "features/dataset": DatasetSchema(
        key="features/dataset",
        required_columns=("license_scope",),
        primary_key=("instrument_id", "feature_asof_timestamp"),
        default_values={"asset_class": "feature_dataset"},
    ),
}


def get_dataset_schema(key: str) -> DatasetSchema:
    if key not in SCHEMA_REGISTRY:
        raise DataValidationError(f"Unknown dataset schema: {key}")
    return SCHEMA_REGISTRY[key]


def empty_frame(key: str) -> pd.DataFrame:
    schema = get_dataset_schema(key)
    columns: list[str] = list(COMMON_COLUMNS)
    for column in schema.required_columns:
        if column not in columns:
            columns.append(column)
    for column in COMMON_OPTIONAL_COLUMNS:
        if column not in columns:
            columns.append(column)
    for column in schema.default_values:
        if column not in columns:
            columns.append(column)
    return pd.DataFrame(columns=columns)


def validate_frame(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    schema = get_dataset_schema(key)
    normalized = frame.copy()
    for column, value in schema.default_values.items():
        if column not in normalized.columns:
            normalized[column] = value
    missing = [column for column in (*COMMON_COLUMNS, *schema.required_columns) if column not in normalized.columns]
    if missing:
        raise DataValidationError(f"Dataset {key} is missing required columns: {', '.join(sorted(missing))}")
    for column in schema.timestamp_columns:
        normalized[column] = pd.to_datetime(normalized[column], utc=True)
    for column in (
        "instrument_id",
        "source_name",
        "asset_class",
        "currency",
        "exchange",
        "board_id",
        "ticker",
        "isin",
        "figi",
        "uid",
        "license_scope",
    ):
        if column in normalized.columns:
            normalized[column] = normalized[column].where(normalized[column].notna(), None)
            normalized[column] = normalized[column].map(lambda value: str(value) if value is not None else None)
    ordered = schema.ordered_columns(normalized)
    deduped = normalized.drop_duplicates(subset=list(schema.primary_key), keep="last")
    return deduped[ordered]
