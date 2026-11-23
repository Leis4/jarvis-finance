from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(slots=True)
class TimeSplit:
    train_index: np.ndarray
    test_index: np.ndarray
    train_end: str
    test_start: str
    test_end: str


def walk_forward_splits(
    dataset: pd.DataFrame,
    n_splits: int = 4,
    min_train_dates: int = 60,
    *,
    gap_dates: int = 1,
    embargo_dates: int = 1,
) -> list[TimeSplit]:
    unique_dates = dataset["feature_asof_timestamp"].drop_duplicates().sort_values().to_list()
    if len(unique_dates) < min_train_dates + n_splits:
        min_train_dates = max(20, len(unique_dates) // 2)
    remaining = len(unique_dates) - min_train_dates
    step = max(1, remaining // max(1, n_splits))
    splits: list[TimeSplit] = []
    safe_gap = max(0, int(gap_dates or 0))
    safe_embargo = max(0, int(embargo_dates or 0))
    for i in range(n_splits):
        train_end_idx = min_train_dates + i * step
        test_start_idx = train_end_idx + safe_gap + safe_embargo
        test_end_idx = min(len(unique_dates), test_start_idx + step)
        if test_end_idx <= test_start_idx:
            continue
        train_dates = set(unique_dates[:train_end_idx])
        test_dates = set(unique_dates[test_start_idx:test_end_idx])
        train_idx = dataset.index[dataset["feature_asof_timestamp"].isin(train_dates)].to_numpy()
        test_idx = dataset.index[dataset["feature_asof_timestamp"].isin(test_dates)].to_numpy()
        if len(train_idx) and len(test_idx):
            splits.append(
                TimeSplit(
                    train_idx,
                    test_idx,
                    str(unique_dates[train_end_idx - 1]),
                    str(unique_dates[test_start_idx]),
                    str(unique_dates[test_end_idx - 1]),
                )
            )
    return splits
