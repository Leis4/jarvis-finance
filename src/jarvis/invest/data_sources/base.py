from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TypeVar

import httpx
import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.core.exceptions import DataValidationError, ExternalServiceError
from jarvis.invest.core.logging import get_logger
from jarvis.invest.core.utils import stable_hash, utc_now

T = TypeVar("T")


class BaseConnector(ABC):
    source_name: str = "base"

    def __init__(
        self,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        rate_limit_seconds: float = 0.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.rate_limit_seconds = rate_limit_seconds
        self.client = client
        self.logger = get_logger(f"data_source.{self.source_name}")

    def _with_retry(self, fn: Callable[[], T]) -> T:
        attempt = 0
        while True:
            try:
                if self.rate_limit_seconds:
                    time.sleep(self.rate_limit_seconds)
                return fn()
            except Exception:
                attempt += 1
                if attempt >= self.max_retries:
                    raise
                time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))

    def _request_json(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        method: str = "GET",
        json_payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, object]:
        def _request() -> dict[str, object]:
            client = self.client or httpx.Client(timeout=timeout, follow_redirects=True)
            should_close = self.client is None
            try:
                response = client.request(method, url, params=params, json=json_payload, headers=headers)  # type: ignore[arg-type]
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ExternalServiceError(f"Unexpected payload from {self.source_name}: expected JSON object.")
                self.logger.debug(
                    "external_request_ok",
                    extra={
                        "extra_fields": {
                            "source_name": self.source_name,
                            "method": method,
                            "url": str(response.url),
                            "status_code": response.status_code,
                        }
                    },
                )
                return payload
            except httpx.HTTPError as exc:
                raise ExternalServiceError(f"{self.source_name} request failed: {exc}") from exc
            finally:
                if should_close:
                    client.close()

        return self._with_retry(_request)

    def normalize_common_columns(self, frame: pd.DataFrame, effective_col: str = "timestamp") -> pd.DataFrame:
        if "instrument_id" not in frame.columns:
            raise DataValidationError("Missing mandatory instrument_id column.")
        if effective_col not in frame.columns:
            raise DataValidationError(f"Missing effective timestamp column: {effective_col}")
        now = utc_now()
        normalized = frame.copy()
        normalized["source_name"] = self.source_name
        normalized["source_timestamp"] = pd.to_datetime(normalized[effective_col], utc=True)
        normalized["ingestion_timestamp"] = now
        normalized["effective_timestamp"] = pd.to_datetime(normalized[effective_col], utc=True)
        normalized["feature_asof_timestamp"] = pd.to_datetime(normalized[effective_col], utc=True)
        normalized["data_version"] = normalized.apply(
            lambda row: stable_hash(
                {"instrument_id": row["instrument_id"], "ts": str(row[effective_col]), "source": self.source_name}
            ),
            axis=1,
        )
        return normalized.drop_duplicates(subset=["instrument_id", "effective_timestamp"])

    @abstractmethod
    def fetch(self) -> dict[str, pd.DataFrame]:
        ...
