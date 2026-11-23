from __future__ import annotations

from datetime import UTC

import httpx
import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.exceptions import ConfigurationError
from jarvis.invest.data_sources.base import BaseConnector


class CbondsDataSource(BaseConnector):
    source_name = "cbonds"

    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__(max_retries=3, backoff_seconds=0.5, rate_limit_seconds=0.5, client=client)
        self.settings = get_settings()
        self.base_url = self.settings.cbonds_base_url.rstrip("/")

    def is_enabled(self) -> bool:
        return bool(self.settings.cbonds_login and self.settings.cbonds_password)

    def _auth_payload(self) -> dict[str, object]:
        if not self.settings.cbonds_login or not self.settings.cbonds_password:
            raise ConfigurationError("Cbonds credentials are not configured.")
        return {"auth": {"login": self.settings.cbonds_login, "password": self.settings.cbonds_password}}

    def fetch_reference(self, limit: int = 200) -> dict[str, pd.DataFrame]:
        if not self.is_enabled():
            return {"emissions": pd.DataFrame(), "quotes_eod": pd.DataFrame(), "analytics": pd.DataFrame()}
        payload = self._request_json(
            f"{self.base_url}/get_emissions/?lang=eng",
            method="POST",
            json_payload={
                **self._auth_payload(),
                "quantity": {"limit": limit, "offset": 0},
                "fields": [
                    {"field": "id"},
                    {"field": "isin_code"},
                    {"field": "name_eng"},
                    {"field": "currency_name"},
                    {"field": "maturity_date"},
                ],
            },
        )
        items = payload.get("items", [])
        if not isinstance(items, list):
            items = []
        emissions = pd.DataFrame(items)
        if emissions.empty:
            return {"emissions": emissions, "quotes_eod": pd.DataFrame(), "analytics": pd.DataFrame()}
        emissions = emissions.rename(
            columns={
                "id": "source_instrument_id",
                "isin_code": "isin",
                "name_eng": "name",
                "currency_name": "currency",
                "maturity_date": "maturity_date",
            }
        )
        emissions["instrument_id"] = emissions["isin"].fillna(
            emissions["source_instrument_id"].map(lambda value: f"CBONDS:{value}")
        )
        emissions["asset_class"] = "bond"
        emissions["exchange"] = "CBONDS"
        emissions["license_scope"] = "subscription_required"
        emissions["timestamp"] = pd.Timestamp.now(tz=UTC)
        emissions = self.normalize_common_columns(emissions, effective_col="timestamp")
        return {"emissions": emissions, "quotes_eod": pd.DataFrame(), "analytics": pd.DataFrame()}

    def fetch(self) -> dict[str, pd.DataFrame]:
        return self.fetch_reference()
