from __future__ import annotations

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from jarvis.invest.features.builders import StandardFeatureBuilder


def _rolling_max_drawdown(close: pd.Series, window: int, *, min_periods: int) -> pd.Series:
    values = pd.to_numeric(close, errors="coerce")

    def apply_fn(x: np.ndarray) -> float:
        if x.size < min_periods:
            return np.nan
        running_max = np.maximum.accumulate(x)
        safe = np.where(running_max == 0, np.nan, x / running_max - 1.0)
        return float(np.nanmin(safe)) if np.isfinite(safe).any() else np.nan

    return values.rolling(window, min_periods=min_periods).apply(apply_fn, raw=True)


class MarketFeaturesV2Builder:
    """
    Practical, explainable, point-in-time safe daily alpha features.

    This builder intentionally avoids any look-ahead inputs. It operates only on
    same-day OHLCV + trailing rolling windows and cross-sectional statistics at the
    same timestamp.
    """

    def __init__(self) -> None:
        self.base = StandardFeatureBuilder()

    def build(
        self,
        market: pd.DataFrame,
        benchmarks: pd.DataFrame,
        sectors: pd.DataFrame,
    ) -> pd.DataFrame:
        features = self.base.build(market, benchmarks, sectors).copy()
        if features.empty:
            return features

        features["timestamp"] = pd.to_datetime(features["effective_timestamp"], utc=True)
        features = features.sort_values(["instrument_id", "timestamp"]).reset_index(drop=True)

        grouped = features.groupby("instrument_id", sort=False)
        close = pd.to_numeric(features["close"], errors="coerce")
        volume = pd.to_numeric(features.get("volume", 0.0), errors="coerce").fillna(0.0)

        features["momentum_5d"] = features["return_5d"]
        features["momentum_20d"] = features["return_20d"]
        features["momentum_60d"] = features["return_60d"]

        # Rolling max drawdown (windowed, not full-history).
        features["max_drawdown_60d"] = grouped["close"].transform(lambda s: _rolling_max_drawdown(s, 60, min_periods=10))
        features["max_drawdown_120d"] = grouped["close"].transform(lambda s: _rolling_max_drawdown(s, 120, min_periods=20))

        # Distance to rolling ranges.
        high_20 = grouped["close"].transform(lambda s: s.rolling(20, min_periods=5).max())
        low_20 = grouped["close"].transform(lambda s: s.rolling(20, min_periods=5).min())
        high_120 = grouped["close"].transform(lambda s: s.rolling(120, min_periods=20).max())
        low_120 = grouped["close"].transform(lambda s: s.rolling(120, min_periods=20).min())
        features["price_vs_20d_high"] = close / high_20 - 1.0
        features["price_vs_20d_low"] = close / low_20 - 1.0
        features["price_vs_120d_high"] = close / high_120 - 1.0
        features["price_vs_120d_low"] = close / low_120 - 1.0

        # Liquidity / turnover stability.
        dollar_volume = close * volume
        features["dollar_volume"] = dollar_volume
        dv_med_5 = grouped["dollar_volume"].transform(lambda s: s.rolling(5, min_periods=3).median())
        dv_med_20 = grouped["dollar_volume"].transform(lambda s: s.rolling(20, min_periods=5).median())
        dv_mean_60 = grouped["dollar_volume"].transform(lambda s: s.rolling(60, min_periods=10).mean())
        dv_std_60 = grouped["dollar_volume"].transform(lambda s: s.rolling(60, min_periods=10).std())
        features["liquidity_deterioration_5v20"] = dv_med_5 / dv_med_20 - 1.0
        features["liquidity_stability_cv_60d"] = dv_std_60 / dv_mean_60.replace(0, np.nan)

        vol_mean_20 = grouped["volume"].transform(lambda s: s.rolling(20, min_periods=5).mean())
        features["volume_ratio_to_avg20"] = volume / vol_mean_20.replace(0, np.nan)
        features["volume_change_1d"] = grouped["volume"].pct_change(fill_method=None)

        # Gap / reversal style features (if OHLC are present).
        open_px = pd.to_numeric(features.get("open", np.nan), errors="coerce")
        prev_close = grouped["close"].shift(1)
        features["gap_open_prev_close"] = open_px / prev_close.replace(0, np.nan) - 1.0
        features["intraday_return"] = close / open_px.replace(0, np.nan) - 1.0
        high_px = pd.to_numeric(features.get("high", np.nan), errors="coerce")
        low_px = pd.to_numeric(features.get("low", np.nan), errors="coerce")
        span = (high_px - low_px).replace(0, np.nan)
        features["close_location"] = (close - low_px) / span

        # Cross-sectional and sector-relative strength.
        features["universe_median_momentum_20d"] = features.groupby("timestamp")["momentum_20d"].transform("median")
        features["momentum_20d_rel_universe"] = features["momentum_20d"] - features["universe_median_momentum_20d"]
        if "sector_name" in features.columns:
            features["sector_median_momentum_20d"] = features.groupby(["timestamp", "sector_name"])["momentum_20d"].transform("median")
            features["momentum_20d_rel_sector"] = features["momentum_20d"] - features["sector_median_momentum_20d"]
        else:
            features["sector_median_momentum_20d"] = np.nan
            features["momentum_20d_rel_sector"] = np.nan

        # Breadth indicators (same-day cross-sectional stats).
        pos_20 = (features["momentum_20d"] > 0).astype(float)
        above_sma50 = (close > pd.to_numeric(features.get("sma_50", np.nan), errors="coerce")).astype(float)
        features["breadth_pct_pos_20d"] = pos_20.groupby(features["timestamp"]).transform("mean")
        features["breadth_pct_above_sma50"] = above_sma50.groupby(features["timestamp"]).transform("mean")
        features["breadth_median_drawdown"] = features.groupby("timestamp")["drawdown"].transform("median")

        # Replace inf and keep point-in-time contract.
        features["feature_asof_timestamp"] = pd.to_datetime(features["feature_asof_timestamp"], utc=True, errors="coerce")
        features = features.replace([np.inf, -np.inf], np.nan)
        return features.dropna(subset=["feature_asof_timestamp", "close"]).reset_index(drop=True)
