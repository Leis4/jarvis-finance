from __future__ import annotations

from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.core.exceptions import ConfigurationError
from jarvis.invest.core.utils import ensure_dir


def _lazy_import_pyarrow() -> None:
    try:
        import pyarrow  # type: ignore[import-untyped]  # noqa: F401
    except Exception as exc:
        raise ConfigurationError("PyArrow is required for parquet I/O.") from exc


def write_frame(frame: pd.DataFrame, path: Path, file_format: str) -> None:
    ensure_dir(path.parent)
    if file_format == "parquet":
        _lazy_import_pyarrow()
        frame.to_parquet(path, index=False)
        return
    if file_format == "csv":
        frame.to_csv(path, index=False)
        return
    raise ConfigurationError(f"Unsupported file format: {file_format}")


def read_frame(path: Path, file_format: str) -> pd.DataFrame:
    if file_format == "parquet":
        _lazy_import_pyarrow()
        return pd.read_parquet(path)
    if file_format == "csv":
        return pd.read_csv(path)
    raise ConfigurationError(f"Unsupported file format: {file_format}")
