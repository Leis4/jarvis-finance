from __future__ import annotations

from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.interfaces import MarketDataRepository
from jarvis.invest.core.utils import ensure_dir
from jarvis.invest.storage.io import read_frame, write_frame


class LocalMarketDataRepository(MarketDataRepository):
    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_dir = ensure_dir(self.settings.data_dir)

    def _path(self, layer: str, name: str) -> Path:
        suffix = "parquet" if self.settings.file_format == "parquet" else "csv"
        return self.base_dir / layer / f"{name}.{suffix}"

    def write_table(self, layer: str, name: str, frame: pd.DataFrame) -> str:
        path = self._path(layer, name)
        write_frame(frame, path, self.settings.file_format)
        return str(path)

    def read_table(self, layer: str, name: str) -> pd.DataFrame:
        return read_frame(self._path(layer, name), self.settings.file_format)

    def latest_snapshot(self, layer: str, name: str) -> pd.DataFrame:
        return self.read_table(layer, name)
