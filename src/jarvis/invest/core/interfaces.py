"""Core interfaces for invest platform."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd  # type: ignore[import-untyped]


@dataclass(slots=True)
class OrderRequest:
    instrument_id: str
    side: str
    quantity: int
    order_type: str
    price: float | None
    account_id: str | None = None
    broker_mode: str = "paper"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderResult:
    order_id: str
    status: str
    instrument_id: str
    side: str
    quantity: int
    requested_price: float | None
    filled_price: float | None
    filled_quantity: int
    broker_mode: str
    submitted_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


class DataSource(ABC):
    @abstractmethod
    def fetch(self) -> dict[str, pd.DataFrame]:
        ...


class MarketDataRepository(ABC):
    @abstractmethod
    def write_table(self, layer: str, name: str, frame: pd.DataFrame) -> str:
        ...

    @abstractmethod
    def read_table(self, layer: str, name: str) -> pd.DataFrame:
        ...

    @abstractmethod
    def latest_snapshot(self, layer: str, name: str) -> pd.DataFrame:
        ...


class FeatureBuilder(ABC):
    @abstractmethod
    def build(self, market: pd.DataFrame, benchmarks: pd.DataFrame, sectors: pd.DataFrame) -> pd.DataFrame:
        ...


class LabelBuilder(ABC):
    @abstractmethod
    def build(self, features: pd.DataFrame) -> pd.DataFrame:
        ...


class ModelTrainer(ABC):
    @abstractmethod
    def train(self, dataset: pd.DataFrame) -> dict[str, Any]:
        ...


class Predictor(ABC):
    @abstractmethod
    def predict(self, dataset: pd.DataFrame) -> pd.DataFrame:
        ...


class Ranker(ABC):
    @abstractmethod
    def rank(self, predictions: pd.DataFrame) -> pd.DataFrame:
        ...


class Backtester(ABC):
    @abstractmethod
    def run(self, features: pd.DataFrame, predictions: pd.DataFrame) -> dict[str, Any]:
        ...


class PaperBroker(ABC):
    @abstractmethod
    def submit_orders(self, orders: list[OrderRequest], market: pd.DataFrame) -> list[OrderResult]:
        ...


class BrokerAdapter(ABC):
    @abstractmethod
    def get_accounts(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_portfolio(self, account_id: str | None = None) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_positions(self, account_id: str | None = None) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_cash_balances(self, account_id: str | None = None) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_instruments(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_quotes(self, instrument_ids: list[str]) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def place_order(self, order: OrderRequest) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str, account_id: str | None = None) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_orders(self, account_id: str | None = None) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_fills(self, account_id: str | None = None) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_operations(self, account_id: str | None = None) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def validate_order(self, order: OrderRequest) -> dict[str, Any]:
        ...

    @abstractmethod
    def healthcheck(self) -> dict[str, Any]:
        ...


class RiskEngine(ABC):
    @abstractmethod
    def validate_orders(self, orders: list[OrderRequest], portfolio: dict[str, Any], market: pd.DataFrame) -> list[OrderRequest]:
        ...


class PortfolioAllocator(ABC):
    @abstractmethod
    def allocate(self, rankings: pd.DataFrame, portfolio: dict[str, Any], cash: float) -> list[OrderRequest]:
        ...


class ReportBuilder(ABC):
    @abstractmethod
    def build(self, payload: dict[str, Any]) -> dict[str, str]:
        ...
