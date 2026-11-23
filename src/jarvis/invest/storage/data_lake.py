from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.utils import ensure_dir
from jarvis.invest.storage.dataset_schemas import (
    empty_frame,
    get_dataset_schema,
    validate_frame,
)
from jarvis.invest.storage.io import read_frame, write_frame


@dataclass(slots=True)
class DatasetInfo:
    key: str
    path: str
    exists: bool
    rows: int
    latest_effective_timestamp: str | None
    latest_ingestion_timestamp: str | None


class DataLakeStore:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_dir = ensure_dir(self.settings.data_dir)
        self.file_format = self.settings.file_format

    def dataset_path(self, key: str) -> Path:
        layer, dataset = key.split("/", 1)
        suffix = "parquet" if self.file_format == "parquet" else "csv"
        return self.base_dir / layer / Path(dataset) / f"latest.{suffix}"

    def exists(self, key: str) -> bool:
        return self.dataset_path(key).exists()

    def read(self, key: str) -> pd.DataFrame:
        path = self.dataset_path(key)
        if not path.exists():
            raise FileNotFoundError(path)
        return read_frame(path, self.file_format)

    def read_or_empty(self, key: str) -> pd.DataFrame:
        path = self.dataset_path(key)
        if not path.exists():
            return empty_frame(key)
        return read_frame(path, self.file_format)

    def write(self, key: str, frame: pd.DataFrame, *, replace: bool = False) -> str:
        validated = validate_frame(frame, key)
        schema = get_dataset_schema(key)
        path = self.dataset_path(key)
        ensure_dir(path.parent)
        if replace or not path.exists():
            final_frame = validated
        else:
            existing = self.read_or_empty(key)
            combined = pd.concat([existing, validated], ignore_index=True)
            final_frame = validate_frame(combined, key)
            final_frame = final_frame.sort_values(list(schema.primary_key)).reset_index(drop=True)
        write_frame(final_frame, path, self.file_format)
        return str(path)

    def write_empty(self, key: str, *, replace: bool = True) -> str:
        return self.write(key, empty_frame(key), replace=replace)

    def info(self, key: str) -> DatasetInfo:
        path = self.dataset_path(key)
        if not path.exists():
            return DatasetInfo(
                key=key,
                path=str(path),
                exists=False,
                rows=0,
                latest_effective_timestamp=None,
                latest_ingestion_timestamp=None,
            )
        frame = read_frame(path, self.file_format)
        latest_effective = None
        latest_ingestion = None
        if not frame.empty:
            if "effective_timestamp" in frame.columns:
                latest_effective = str(pd.to_datetime(frame["effective_timestamp"], utc=True).max())
            if "ingestion_timestamp" in frame.columns:
                latest_ingestion = str(pd.to_datetime(frame["ingestion_timestamp"], utc=True).max())
        return DatasetInfo(
            key=key,
            path=str(path),
            exists=True,
            rows=len(frame),
            latest_effective_timestamp=latest_effective,
            latest_ingestion_timestamp=latest_ingestion,
        )
