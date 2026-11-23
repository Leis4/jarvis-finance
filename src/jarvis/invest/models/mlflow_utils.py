from __future__ import annotations

from typing import Any, Self


class DummyRun:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        pass


class MLflowLogger:
    def __init__(self, tracking_uri: str | None = None) -> None:
        self.tracking_uri = tracking_uri

    def start_run(self, run_name: str | None = None) -> DummyRun:
        return DummyRun()

    def log_params(self, params: dict[str, Any]) -> None:
        pass

    def log_metrics(self, metrics: dict[str, float], step: int | None = None) -> None:
        pass

    def log_artifact(self, local_path: str) -> None:
        pass
