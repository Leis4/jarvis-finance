from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.exceptions import ConfigurationError
from jarvis.invest.core.utils import ensure_dir


class DuckDBQueryEngine:
    def __init__(self, db_path: Path | None = None) -> None:
        self.settings = get_settings()
        self.db_path = db_path or ensure_dir(self.settings.artifacts_dir) / "investing.duckdb"

    @staticmethod
    def _connect() -> Any:
        try:
            import duckdb
        except Exception as exc:
            raise ConfigurationError("DuckDB is required when USE_DUCKDB=true.") from exc
        return duckdb

    def query(self, sql: str) -> pd.DataFrame:
        duckdb = self._connect()
        with duckdb.connect(str(self.db_path)) as con:
            return con.execute(sql).df()

    def execute(self, sql: str) -> None:
        duckdb = self._connect()
        with duckdb.connect(str(self.db_path)) as con:
            con.execute(sql)
