from __future__ import annotations

from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.data_sources.base import BaseConnector


class SampleMarketDataSource(BaseConnector):
    source_name = "sample_data"

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(max_retries=2, backoff_seconds=0.1)
        self.root = root or (Path(__file__).resolve().parents[4] / "_legacy" / "invest_platform" / "sample_data")

    def fetch(self) -> dict[str, pd.DataFrame]:
        market = pd.read_csv(self.root / "market_daily.csv")
        benchmark = pd.read_csv(self.root / "benchmark_daily.csv")
        sector = pd.read_csv(self.root / "sector_daily.csv")
        fundamentals = pd.read_csv(self.root / "fundamentals.csv")
        sentiment = pd.read_csv(self.root / "sentiment_daily.csv")
        corporate_actions = pd.read_csv(self.root / "corporate_actions.csv")
        return {
            "market": self.normalize_common_columns(market, effective_col="timestamp"),
            "benchmark": self.normalize_common_columns(benchmark, effective_col="timestamp"),
            "sector": self.normalize_common_columns(sector, effective_col="timestamp"),
            "fundamentals": self.normalize_common_columns(fundamentals, effective_col="timestamp"),
            "sentiment": self.normalize_common_columns(sentiment, effective_col="timestamp"),
            "corporate_actions": self.normalize_common_columns(corporate_actions, effective_col="timestamp"),
        }
