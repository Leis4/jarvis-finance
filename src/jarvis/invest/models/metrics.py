from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    mean_absolute_error,
    mean_squared_error,
    precision_score,
    roc_auc_score,
)


def classification_metrics(y_true: np.ndarray, y_prob: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    return {
        "roc_auc": float(roc_auc_score(y_true, y_prob)) if len(np.unique(y_true)) > 1 else 0.5,
        "pr_auc": float(average_precision_score(y_true, y_prob)),
        "log_loss": float(log_loss(y_true, np.vstack([1 - y_prob, y_prob]).T, labels=[0, 1])),
        "brier_score": float(brier_score_loss(y_true, y_prob)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
    }


def regression_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    return {"rmse": float(rmse), "mae": float(mean_absolute_error(y_true, y_pred))}


def rank_metrics(frame: pd.DataFrame, score_col: str, target_col: str, top_k: int) -> dict[str, float]:
    ranked = frame.sort_values(["feature_asof_timestamp", score_col], ascending=[True, False]).copy()
    ranked["rank_within_day"] = ranked.groupby("feature_asof_timestamp")[score_col].rank(method="first", ascending=False)
    top = ranked[ranked["rank_within_day"] <= top_k]
    precision_at_k = top[target_col].mean() if not top.empty else 0.0
    res = spearmanr(ranked[score_col], ranked[target_col], nan_policy="omit")
    rank_ic = getattr(res, "statistic", getattr(res, "correlation", 0.0))
    if rank_ic is None or np.isnan(rank_ic):
        rank_ic = 0.0
    return {
        "precision_at_k": float(precision_at_k) if not np.isnan(precision_at_k) else 0.0,
        "rank_ic": float(rank_ic),
    }
