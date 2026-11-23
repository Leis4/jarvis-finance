"""Configuration settings for invest platform."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass
class Settings:
    artifacts_dir: Path = Path("artifacts")
    paper_initial_cash: float = 1_000_000.0
    paper_slippage_bps: float = 5.0
    paper_fee_bps: float = 3.0
    risk_max_position_pct: float = 0.10
    risk_max_gross_exposure: float = 1.00
    risk_max_sector_exposure: float = 0.35
    risk_max_daily_turnover: float = 0.40
    risk_liquidity_fraction: float = 0.10
    risk_max_concurrent_positions: int = 10
    risk_min_daily_dollar_volume: float = 500_000.0
    moex_iss_base_url: str = "https://iss.moex.com"
    cbonds_base_url: str = "https://ws.cbonds.info/services/json"
    cbonds_login: str | None = None
    cbonds_password: str | None = None
    data_dir: Path = Path("data_lake")
    database_url: str = "sqlite:///artifacts/local.db"
    file_format: str = "parquet"
    use_duckdb: bool = True
    model_seed: int = 42
    model_name: str = "logistic_regression"
    default_top_k: int = 3
    default_horizon_days: int = 5
    feature_bundle: str = "BASE_FEATURES"
    target_bundle: str = "TARGET_PEER_MEDIAN_H5"
    mlflow_tracking_uri: str = "sqlite:///artifacts/mlflow.db"
    target_absolute_return_threshold: float = 0.0
    max_prediction_staleness_hours: float = 168.0
    news_feature_mode: str = "disabled"
    enable_event_features: bool = False
    enable_news_features: bool = False
    enable_sentiment_features: bool = False
    broker_mode: str = "paper"
    portfolio_plan_path: Path = Path("configs/portfolio/default.yaml")
    allocation_universe_policy_path: Path = Path("configs/universe/default.yaml")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
