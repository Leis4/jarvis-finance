from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]


def _talib() -> Any:
    try:
        import talib  # type: ignore[import-not-found, unused-ignore]
        return talib
    except ImportError:
        return None


def sma(series: pd.Series, period: int) -> pd.Series:
    t_lib = _talib()
    if t_lib is not None:
        return pd.Series(t_lib.SMA(series.values.astype(float), timeperiod=period), index=series.index)
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    t_lib = _talib()
    if t_lib is not None:
        return pd.Series(t_lib.EMA(series.values.astype(float), timeperiod=period), index=series.index)
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    t_lib = _talib()
    if t_lib is not None:
        return pd.Series(t_lib.RSI(series.values.astype(float), timeperiod=period), index=series.index)
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    t_lib = _talib()
    if t_lib is not None:
        m, s, h = t_lib.MACD(series.values.astype(float), fastperiod=12, slowperiod=26, signalperiod=9)
        return pd.Series(m, index=series.index), pd.Series(s, index=series.index), pd.Series(h, index=series.index)
    fast = ema(series, 12)
    slow = ema(series, 26)
    m = fast - slow
    s = m.ewm(span=9, adjust=False).mean()
    return m, s, m - s


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    t_lib = _talib()
    if t_lib is not None:
        values = t_lib.ATR(high.values.astype(float), low.values.astype(float), close.values.astype(float), timeperiod=period)
        return pd.Series(values, index=close.index)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()
