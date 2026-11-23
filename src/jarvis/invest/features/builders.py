from __future__ import annotations

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.core.interfaces import FeatureBuilder
from jarvis.invest.features.indicators import atr, ema, macd, rsi, sma


class StandardFeatureBuilder(FeatureBuilder):
    def build(self, market: pd.DataFrame, benchmarks: pd.DataFrame, sectors: pd.DataFrame) -> pd.DataFrame:
        market = market.copy()
        market["timestamp"] = pd.to_datetime(market["effective_timestamp"], utc=True)
        benchmarks = benchmarks.copy()
        benchmarks["timestamp"] = pd.to_datetime(benchmarks["effective_timestamp"], utc=True)
        sectors = sectors.copy()
        sectors["timestamp"] = pd.to_datetime(sectors["effective_timestamp"], utc=True)

        benchmark_returns = benchmarks.sort_values(["instrument_id", "timestamp"]).copy()
        benchmark_returns["benchmark_return_1d"] = benchmark_returns.groupby("instrument_id")["close"].pct_change(fill_method=None)
        benchmark_returns = benchmark_returns[["timestamp", "benchmark_return_1d", "close"]].rename(columns={"close": "benchmark_close"})

        sector_returns = sectors.sort_values(["instrument_id", "timestamp"]).copy()
        sector_returns["sector_return_1d"] = sector_returns.groupby("instrument_id")["close"].pct_change(fill_method=None)
        sector_returns = sector_returns[["timestamp", "sector_name", "sector_return_1d", "close"]].rename(columns={"close": "sector_close"})

        market = market.sort_values(["instrument_id", "timestamp"]).merge(benchmark_returns, on="timestamp", how="left")
        market = market.merge(sector_returns, on=["timestamp", "sector_name"], how="left")

        def build_group(group: pd.DataFrame) -> pd.DataFrame:
            g = group.sort_values("timestamp").copy()
            g["return_1d"] = g["close"].pct_change(fill_method=None)
            g["return_5d"] = g["close"].pct_change(5, fill_method=None)
            g["return_10d"] = g["close"].pct_change(10, fill_method=None)
            g["return_20d"] = g["close"].pct_change(20, fill_method=None)
            g["return_60d"] = g["close"].pct_change(60, fill_method=None)
            g["rolling_vol_10d"] = g["return_1d"].rolling(10).std()
            g["rolling_vol_20d"] = g["return_1d"].rolling(20).std()
            g["rolling_vol_60d"] = g["return_1d"].rolling(60).std()
            g["volatility_10d"] = g["return_1d"].rolling(10).std()
            g["volatility_20d"] = g["return_1d"].rolling(20).std()
            g["volatility_60d"] = g["return_1d"].rolling(60).std()
            g["atr_14"] = atr(g["high"], g["low"], g["close"], 14)
            g["rsi_14"] = rsi(g["close"], 14)
            macd_line, macd_signal, macd_hist = macd(g["close"])
            g["macd"] = macd_line
            g["macd_signal"] = macd_signal
            g["macd_hist"] = macd_hist
            g["sma_10"] = sma(g["close"], 10)
            g["sma_20"] = sma(g["close"], 20)
            g["sma_50"] = sma(g["close"], 50)
            g["ema_10"] = ema(g["close"], 10)
            g["ema_20"] = ema(g["close"], 20)
            g["ema_50"] = ema(g["close"], 50)
            g["ema_gap_10_20"] = g["ema_10"] - g["ema_20"]
            g["price_vs_sma50"] = g["close"] / g["sma_50"] - 1.0
            g["price_vs_60d_high"] = g["close"] / g["close"].rolling(60, min_periods=5).max() - 1.0
            g["price_vs_60d_low"] = g["close"] / g["close"].rolling(60, min_periods=5).min() - 1.0
            rolling_volume = g["volume"].rolling(20)
            g["volume_zscore_20d"] = (g["volume"] - rolling_volume.mean()) / rolling_volume.std()
            g["volume_zscore_60d"] = (g["volume"] - g["volume"].rolling(60).mean()) / g["volume"].rolling(60).std()
            rolling_max = g["close"].cummax()
            g["drawdown"] = g["close"] / rolling_max - 1.0
            cov = g["return_1d"].rolling(20).cov(g["benchmark_return_1d"])
            var = g["benchmark_return_1d"].rolling(20).var()
            g["rolling_beta_20d"] = cov / var.replace(0, np.nan)
            g["benchmark_relative_strength"] = g["return_20d"] - g["benchmark_close"].pct_change(20, fill_method=None)
            g["sector_relative_strength"] = g["return_20d"] - g["sector_close"].pct_change(20, fill_method=None)
            g["calendar_day_of_week"] = g["timestamp"].dt.dayofweek
            g["calendar_month_end"] = g["timestamp"].dt.is_month_end.astype(int)
            g["liquidity_dollar_volume"] = g["close"] * g["volume"]
            g["spread_proxy"] = (g["high"] - g["low"]) / g["close"].replace(0, np.nan)
            g["volatility_regime_high"] = (g["rolling_vol_20d"] > g["rolling_vol_20d"].rolling(60).median()).astype(int)
            for col in [
                "return_1d",
                "return_5d",
                "return_10d",
                "return_20d",
                "return_60d",
                "rolling_vol_10d",
                "rolling_vol_20d",
                "rolling_vol_60d",
                "volatility_10d",
                "volatility_20d",
                "volatility_60d",
                "atr_14",
                "rsi_14",
                "macd",
                "macd_signal",
                "macd_hist",
                "sma_10",
                "sma_20",
                "sma_50",
                "ema_10",
                "ema_20",
                "ema_50",
                "ema_gap_10_20",
                "volume_zscore_20d",
                "volume_zscore_60d",
                "drawdown",
                "rolling_beta_20d",
                "benchmark_relative_strength",
                "sector_relative_strength",
                "liquidity_dollar_volume",
                "spread_proxy",
                "price_vs_sma50",
                "price_vs_60d_high",
                "price_vs_60d_low",
            ]:
                g[f"{col}_missing"] = g[col].isna().astype(int)
            g["feature_asof_timestamp"] = g["timestamp"]
            return g

        grouped = [build_group(group) for _, group in market.groupby("instrument_id", sort=False)]
        features = pd.concat(grouped, ignore_index=True) if grouped else market.iloc[0:0].copy()
        features["cross_sectional_return_60d_rank"] = features.groupby("feature_asof_timestamp")["return_60d"].rank(method="dense", pct=True, ascending=True)
        features["cross_sectional_volume_rank"] = features.groupby("feature_asof_timestamp")["volume_zscore_20d"].rank(method="dense", pct=True, ascending=True)
        return features.replace([np.inf, -np.inf], np.nan).dropna(subset=["feature_asof_timestamp", "close"])
