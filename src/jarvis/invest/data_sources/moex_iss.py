from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.config.settings import get_settings
from jarvis.invest.data_sources.base import BaseConnector


class MoexIssDataSource(BaseConnector):
    source_name = "moex_iss"

    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__(max_retries=3, backoff_seconds=0.5, rate_limit_seconds=0.2, client=client)
        self.settings = get_settings()
        self.base_url = self.settings.moex_iss_base_url.rstrip("/")
        self.license_scope = "delayed_public_reference"

    @staticmethod
    def _block_frame(payload: dict[str, object], block_name: str) -> pd.DataFrame:
        block = payload.get(block_name, {})
        if not isinstance(block, dict):
            return pd.DataFrame()
        columns = block.get("columns", [])
        data = block.get("data", [])
        if not isinstance(columns, list) or not isinstance(data, list):
            return pd.DataFrame()
        return pd.DataFrame(data, columns=columns)

    @staticmethod
    def _effective_ts(value: object) -> pd.Timestamp:
        if value is None or value == "":
            return pd.Timestamp.now(tz=UTC)
        return pd.to_datetime(value, utc=True)

    @staticmethod
    def _quote_ts(value: object) -> pd.Timestamp:
        moscow_tz = ZoneInfo("Europe/Moscow")
        if value is None or value == "":
            return pd.Timestamp.now(tz=moscow_tz).tz_convert(UTC)
        raw = str(value)
        parsed = pd.to_datetime(raw, format="%H:%M:%S", errors="coerce")
        if pd.notna(parsed):
            now_moscow = datetime.now(moscow_tz)
            localized = datetime(
                now_moscow.year,
                now_moscow.month,
                now_moscow.day,
                parsed.hour,
                parsed.minute,
                parsed.second,
                tzinfo=moscow_tz,
            )
            return pd.Timestamp(localized).tz_convert(UTC)
        return pd.to_datetime(raw, utc=True)

    def _universe_request(self, market: str, board_id: str) -> dict[str, pd.DataFrame]:
        payload = self._request_json(
            f"{self.base_url}/iss/engines/stock/markets/{market}/boards/{board_id}/securities.json",
            params={
                "iss.meta": "off",
                "iss.only": "securities,marketdata",
                "securities.columns": "SECID,SHORTNAME,ISIN,BOARDID,LOTSIZE,DECIMALS",
                "marketdata.columns": "SECID,BOARDID,LAST,UPDATETIME,NUMTRADES,VOLTODAY,VALTODAY",
            },
        )
        securities = self._block_frame(payload, "securities")
        marketdata = self._block_frame(payload, "marketdata")
        asset_class = "equity" if market == "shares" else "bond"
        if not securities.empty:
            securities = securities.rename(
                columns={
                    "SECID": "ticker",
                    "SHORTNAME": "name",
                    "ISIN": "isin",
                    "BOARDID": "board_id",
                    "LOTSIZE": "lot_size",
                }
            )
            securities["source_instrument_id"] = securities["ticker"]
            securities["instrument_id"] = securities["ticker"].astype(str).str.upper()
            securities["asset_class"] = asset_class
            securities["exchange"] = "MOEX"
            securities["currency"] = "RUB"
            securities["license_scope"] = self.license_scope
            securities["timestamp"] = pd.Timestamp.now(tz=UTC)
            securities = self.normalize_common_columns(securities, effective_col="timestamp")
        if not marketdata.empty:
            marketdata = marketdata.rename(
                columns={
                    "SECID": "ticker",
                    "BOARDID": "board_id",
                    "LAST": "price",
                    "UPDATETIME": "update_time",
                    "VOLTODAY": "volume",
                    "VALTODAY": "value",
                }
            )
            marketdata["instrument_id"] = marketdata["ticker"].astype(str).str.upper()
            marketdata["asset_class"] = asset_class
            marketdata["exchange"] = "MOEX"
            marketdata["currency"] = "RUB"
            marketdata["license_scope"] = self.license_scope
            marketdata["is_delayed"] = True
            marketdata["timestamp"] = marketdata["update_time"].map(self._quote_ts)
            marketdata["close"] = marketdata["price"]
            marketdata = self.normalize_common_columns(marketdata, effective_col="timestamp")
        return {"securities": securities, "quotes": marketdata}

    def fetch_boards(self) -> pd.DataFrame:
        rows: list[dict[str, object]] = []
        for market, board_id in (("shares", "TQBR"), ("bonds", "TQOB")):
            payload = self._request_json(
                f"{self.base_url}/iss/engines/stock/markets/{market}/boards.json",
                params={"iss.meta": "off", "iss.only": "boards", "boards.columns": "boardid,title"},
            )
            frame = self._block_frame(payload, "boards")
            if frame.empty:
                continue
            now = pd.Timestamp.now(tz=UTC)
            for _, item in frame.iterrows():
                current_board = str(item.get("boardid", "")).upper()
                if current_board != board_id:
                    continue
                rows.append(
                    {
                        "instrument_id": current_board,
                        "board_id": current_board,
                        "board_title": item.get("title"),
                        "market": market,
                        "engine": "stock",
                        "exchange": "MOEX",
                        "license_scope": self.license_scope,
                        "timestamp": now,
                    }
                )
        boards = pd.DataFrame(rows)
        if boards.empty:
            return boards
        return self.normalize_common_columns(boards, effective_col="timestamp")

    def fetch_universe(self) -> dict[str, pd.DataFrame]:
        equity = self._universe_request("shares", "TQBR")
        bonds = self._universe_request("bonds", "TQOB")
        securities = pd.concat([equity["securities"], bonds["securities"]], ignore_index=True)
        quotes = pd.concat([equity["quotes"], bonds["quotes"]], ignore_index=True)
        return {"securities": securities, "boards": self.fetch_boards(), "quotes": quotes}

    def _history_frame(self, ticker: str, board_id: str, market: str, from_date: str, till_date: str) -> pd.DataFrame:
        history_frames: list[pd.DataFrame] = []
        start = 0
        while True:
            payload = self._request_json(
                f"{self.base_url}/iss/history/engines/stock/markets/{market}/boards/{board_id}/securities/{ticker}.json",
                params={"iss.meta": "off", "from": from_date, "till": till_date, "start": start},
            )
            history = self._block_frame(payload, "history")
            if history.empty:
                break
            history_frames.append(history)
            if len(history) < 100:
                break
            start += len(history)
        if not history_frames:
            return pd.DataFrame()
        history = pd.concat(history_frames, ignore_index=True)
        history = history.rename(
            columns={
                "SECID": "ticker",
                "BOARDID": "board_id",
                "TRADEDATE": "trade_date",
                "SHORTNAME": "name",
                "OPEN": "open",
                "LOW": "low",
                "HIGH": "high",
                "CLOSE": "close",
                "VOLUME": "volume",
                "VALUE": "value",
                "CURRENCYID": "currency",
            }
        )
        history["instrument_id"] = history["ticker"].astype(str).str.upper()
        history["exchange"] = "MOEX"
        history["asset_class"] = "equity" if market == "shares" else "bond"
        history["price"] = history["close"]
        history["license_scope"] = self.license_scope
        history["timestamp"] = pd.to_datetime(history["trade_date"], utc=True)
        return self.normalize_common_columns(history, effective_col="timestamp")

    def watchlist(self) -> list[str]:
        tickers: set[str] = set()
        return sorted(tickers)

    def fetch_daily_history(self, tickers: list[str] | None = None, lookback_days: int = 400) -> pd.DataFrame:
        selected = tickers or self.watchlist()
        till = datetime.now(UTC).date().isoformat()
        from_date = (datetime.now(UTC) - timedelta(days=lookback_days)).date().isoformat()
        frames: list[pd.DataFrame] = []
        bond_ids = {ticker for ticker in selected if str(ticker).upper().startswith("SU")}
        for ticker in selected:
            market = "bonds" if ticker in bond_ids else "shares"
            board_id = "TQOB" if market == "bonds" else "TQBR"
            frame = self._history_frame(str(ticker).upper(), board_id, market, from_date, till)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def fetch(self) -> dict[str, pd.DataFrame]:
        universe = self.fetch_universe()
        universe["candles_daily"] = self.fetch_daily_history()
        universe["candles_intraday"] = pd.DataFrame()
        universe["trades"] = pd.DataFrame()
        return universe
