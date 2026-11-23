from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pandas as pd
from sklearn.isotonic import IsotonicRegression

from jarvis.invest.config.settings import get_settings
from jarvis.invest.models.baseline import BaselineModelTrainer
from jarvis.invest.models.catboost_track import CatBoostModelTrainer
from jarvis.invest.storage.data_lake import DataLakeStore

PREDICTION_DATASET_KEY = "silver/model_predictions"
PRIMARY_METRIC_NAME = "precision_at_k"
TARGET_NAME_TEMPLATE = "outperform_cross_sectional_median_{horizon}d"
TARGET_BUNDLE_TEMPLATE = "TARGET_PEER_MEDIAN_H{horizon}"
PREDICTION_COLUMNS = [
    "instrument_id",
    "source_name",
    "source_timestamp",
    "ingestion_timestamp",
    "effective_timestamp",
    "feature_asof_timestamp",
    "data_version",
    "model_name",
    "model_family",
    "model_version",
    "feature_bundle",
    "target_bundle",
    "target_name",
    "score",
    "probability",
    "raw_probability",
    "calibrated_probability",
    "confidence",
    "rank",
    "training_run_id",
    "artifact_id",
]


def _build_deep_trainer(*, architecture: str) -> Any:
    from jarvis.invest.models.deep import DeepModelTrainer

    return DeepModelTrainer(architecture=architecture)


def _settings() -> Any:
    return get_settings()


def _registry_path() -> Path:
    return cast(Path, _settings().artifacts_dir / "models" / "model_status_latest.json")


def _training_runs_dir() -> Path:
    return cast(Path, _settings().artifacts_dir / "models" / "training_runs")


def _archive_root() -> Path:
    return cast(Path, _settings().artifacts_dir / "models" / "archive")


def _prediction_artifact_path(model_family: str, training_run_id: str) -> Path:
    return cast(Path, _settings().artifacts_dir / "predictions" / f"model_predictions_{model_family}_{training_run_id}.csv")


def _legacy_model_dir(model_family: str) -> Path | None:
    settings = _settings()
    models_root = settings.artifacts_dir / "models"
    if model_family == "sklearn":
        candidate = models_root / settings.model_name
        return cast(Path, candidate) if candidate.exists() else None
    for candidate_name in ("mlp", "gru", "cnn", "transformer"):
        candidate = models_root / candidate_name
        if candidate.exists():
            return cast(Path, candidate)
    return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return str(path)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(cast(Any, value) or 0.0)
    except (ValueError, TypeError):
        return default


def _load_history_bars(asof: str | None = None) -> pd.DataFrame:
    store = DataLakeStore()
    bars = store.read_or_empty("silver/daily_bars").copy()
    if bars.empty:
        bars = store.read_or_empty("silver/market").copy()
    if bars.empty:
        return bars
    bars["instrument_id"] = bars["instrument_id"].astype(str).str.upper()
    bars["effective_timestamp"] = pd.to_datetime(bars["effective_timestamp"], utc=True)
    bars["feature_asof_timestamp"] = pd.to_datetime(
        bars.get("feature_asof_timestamp", bars["effective_timestamp"]),
        utc=True,
    )
    if asof is not None:
        anchor = pd.to_datetime(asof, utc=True)
        bars = bars[bars["effective_timestamp"] <= anchor]
    bars["close"] = pd.to_numeric(bars.get("close", bars.get("price", 0.0)), errors="coerce")
    bars["price"] = pd.to_numeric(bars.get("price", bars["close"]), errors="coerce").fillna(bars["close"])
    bars["volume"] = pd.to_numeric(bars.get("volume", 0.0), errors="coerce").fillna(0.0)
    return bars.sort_values(["instrument_id", "effective_timestamp"]).reset_index(drop=True)


def _resolved_bundle(feature_bundle: str | None) -> str:
    settings = _settings()
    if feature_bundle is None:
        return str(settings.feature_bundle or "BASE_FEATURES").strip().upper()
    normalized = str(feature_bundle).strip().upper()
    allowed = {
        "BASE_FEATURES",
        "MARKET_FEATURES_V2",
        "SHARES_PLUS_BONDS_FEATURES_V1",
        "MARKET_PLUS_EVENTS",
        "MARKET_PLUS_NEWS_SENTIMENT",
    }
    return normalized if normalized in allowed else str(settings.feature_bundle or "BASE_FEATURES").strip().upper()


def _parse_target_bundle(target_bundle: str | None) -> tuple[str, int]:
    settings = _settings()
    raw = str(target_bundle or settings.target_bundle or TARGET_BUNDLE_TEMPLATE.format(horizon=settings.default_horizon_days)).strip().upper()
    horizon = int(settings.default_horizon_days)
    if "_H" in raw:
        try:
            suffix = raw.split("_H", 1)[1]
            digits = ""
            for ch in suffix:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                horizon = int(digits)
        except (ValueError, TypeError, IndexError):
            horizon = int(settings.default_horizon_days)
    return raw, horizon


def _target_bundle_name(target_bundle: str | None, *, horizon: int) -> str:
    raw = str(target_bundle or "").strip().upper()
    if raw:
        return raw
    return TARGET_BUNDLE_TEMPLATE.format(horizon=horizon)


def _benchmark_forward_return_by_date(dataset: pd.DataFrame, horizon: int) -> pd.Series:
    if "benchmark_close" not in dataset.columns:
        return pd.Series([pd.NA] * len(dataset), index=dataset.index, dtype="float64")
    dates = pd.to_datetime(dataset["feature_asof_timestamp"], utc=True, errors="coerce")
    bench = pd.to_numeric(dataset["benchmark_close"], errors="coerce")
    bench_by_date = pd.DataFrame({"date": dates, "benchmark_close": bench}).dropna().drop_duplicates(subset=["date"]).sort_values("date")
    if bench_by_date.empty:
        return pd.Series([pd.NA] * len(dataset), index=dataset.index, dtype="float64")
    bench_by_date["benchmark_future_close"] = bench_by_date["benchmark_close"].shift(-horizon)
    bench_by_date["benchmark_forward_return_h"] = bench_by_date["benchmark_future_close"] / bench_by_date["benchmark_close"] - 1.0
    merged = pd.DataFrame({"date": dates}).merge(bench_by_date[["date", "benchmark_forward_return_h"]], on="date", how="left")
    return pd.to_numeric(merged["benchmark_forward_return_h"], errors="coerce")


def _sector_median_expected_return(dataset: pd.DataFrame) -> pd.Series:
    if "sector_name" not in dataset.columns:
        return pd.Series([pd.NA] * len(dataset), index=dataset.index, dtype="float64")
    return dataset.groupby(["feature_asof_timestamp", "sector_name"])["expected_return_h"].transform("median")


def _forward_drawdown_over_horizon(dataset: pd.DataFrame, horizon: int) -> pd.Series:
    if dataset.empty:
        return pd.Series(dtype="float64")
    grouped = dataset.groupby("instrument_id", sort=False)

    def apply_group(series: pd.Series) -> pd.Series:
        base = pd.to_numeric(series, errors="coerce")
        future = base.shift(-1)
        future_min = future.iloc[::-1].rolling(horizon, min_periods=horizon).min().iloc[::-1]
        return future_min / base - 1.0

    return grouped["close"].transform(apply_group)


def _apply_target_bundle(dataset: pd.DataFrame, *, target_bundle: str, horizon: int) -> tuple[pd.DataFrame, str, list[str]]:
    warnings: list[str] = []
    bundle = str(target_bundle).strip().upper()
    label_name = TARGET_NAME_TEMPLATE.format(horizon=horizon)

    dataset = dataset.copy()
    dataset["future_close"] = dataset.groupby("instrument_id")["close"].shift(-horizon)
    dataset["expected_return_h"] = dataset["future_close"] / dataset["close"] - 1.0

    if bundle.startswith("TARGET_PEER_MEDIAN_H"):
        dataset["peer_median_return_h"] = dataset.groupby("feature_asof_timestamp")["expected_return_h"].transform("median")
        dataset["target_outperform"] = (dataset["expected_return_h"] > dataset["peer_median_return_h"]).astype(int)
        label_name = f"outperform_cross_sectional_median_{horizon}d"
    elif bundle.startswith("TARGET_BENCHMARK_H"):
        benchmark_forward = _benchmark_forward_return_by_date(dataset, horizon)
        dataset["benchmark_forward_return_h"] = benchmark_forward
        missing = benchmark_forward.isna().mean() if len(benchmark_forward) else 1.0
        if missing > 0.25:
            warnings.append("Benchmark target: benchmark_close недоступен для значимой части дат; fallback на peer median.")
            dataset["peer_median_return_h"] = dataset.groupby("feature_asof_timestamp")["expected_return_h"].transform("median")
            dataset["target_outperform"] = (dataset["expected_return_h"] > dataset["peer_median_return_h"]).astype(int)
            label_name = f"outperform_cross_sectional_median_{horizon}d"
        else:
            dataset["target_outperform"] = (dataset["expected_return_h"] > dataset["benchmark_forward_return_h"]).astype(int)
            label_name = f"outperform_benchmark_{horizon}d"
    elif bundle.startswith("TARGET_SECTOR_MEDIAN_H"):
        sector_median = _sector_median_expected_return(dataset)
        dataset["sector_median_return_h"] = sector_median
        missing = sector_median.isna().mean() if len(sector_median) else 1.0
        if missing > 0.25:
            warnings.append("Sector-median target: sector_name недоступен для значимой части дат; fallback на peer median.")
            dataset["peer_median_return_h"] = dataset.groupby("feature_asof_timestamp")["expected_return_h"].transform("median")
            dataset["target_outperform"] = (dataset["expected_return_h"] > dataset["peer_median_return_h"]).astype(int)
            label_name = f"outperform_cross_sectional_median_{horizon}d"
        else:
            dataset["target_outperform"] = (dataset["expected_return_h"] > dataset["sector_median_return_h"]).astype(int)
            label_name = f"outperform_sector_median_{horizon}d"
    elif bundle.startswith("TARGET_ABSOLUTE_RETURN_H"):
        settings = _settings()
        threshold = float(settings.target_absolute_return_threshold or 0.0)
        dataset["absolute_return_threshold_h"] = threshold
        dataset["target_outperform"] = (dataset["expected_return_h"] >= threshold).astype(int)
        label_name = f"absolute_return_ge_{threshold:.4f}_{horizon}d"
    elif bundle.startswith("TARGET_TOP_K_H"):
        settings = _settings()
        top_k = int(settings.default_top_k)
        dataset["forward_rank_h"] = dataset.groupby("feature_asof_timestamp")["expected_return_h"].rank(method="first", ascending=False)
        dataset["target_outperform"] = (dataset["forward_rank_h"] <= float(top_k)).astype(int)
        label_name = f"top_k_{top_k}_forward_return_{horizon}d"
    elif bundle.startswith("TARGET_DOWNSIDE_AWARE_H"):
        forward_dd = _forward_drawdown_over_horizon(dataset, horizon)
        dataset["forward_drawdown_h"] = forward_dd
        dataset["peer_median_return_h"] = dataset.groupby("feature_asof_timestamp")["expected_return_h"].transform("median")
        dd_threshold = -0.10
        dataset["downside_dd_threshold_h"] = dd_threshold
        dataset["target_outperform"] = (
            (dataset["expected_return_h"] > dataset["peer_median_return_h"]) & (dataset["forward_drawdown_h"] >= dd_threshold)
        ).astype(int)
        label_name = f"downside_aware_peer_median_{horizon}d"
    else:
        warnings.append(f"Неизвестный target bundle {bundle}; fallback на peer median.")
        dataset["peer_median_return_h"] = dataset.groupby("feature_asof_timestamp")["expected_return_h"].transform("median")
        dataset["target_outperform"] = (dataset["expected_return_h"] > dataset["peer_median_return_h"]).astype(int)
        label_name = f"outperform_cross_sectional_median_{horizon}d"

    dataset = dataset.dropna(subset=["expected_return_h", "feature_asof_timestamp"]).reset_index(drop=True)
    return dataset, label_name, warnings


def _fit_isotonic_calibration(cv_predictions_path: str | None) -> dict[str, Any]:
    if not cv_predictions_path:
        return {"status": "missing", "method": None}
    path = Path(str(cv_predictions_path))
    if not path.exists():
        return {"status": "missing", "method": None}
    frame = pd.read_csv(path)
    if frame.empty or "predicted_probability" not in frame.columns or "target_outperform" not in frame.columns:
        return {"status": "missing", "method": None}
    y = pd.to_numeric(frame["target_outperform"], errors="coerce").fillna(0).astype(int).to_numpy()
    p = pd.to_numeric(frame["predicted_probability"], errors="coerce").fillna(0.5).clip(0.0, 1.0).to_numpy()
    if len(set(y.tolist())) < 2:
        return {"status": "skipped", "method": None, "reason": "target_is_constant"}
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(p, y)
    calibrated = iso.transform(p)
    brier_raw = float(((p - y) ** 2).mean())
    brier_cal = float(((calibrated - y) ** 2).mean())
    return {
        "status": "completed",
        "method": "isotonic",
        "x": iso.X_thresholds_.tolist(),
        "y": iso.y_thresholds_.tolist(),
        "metrics": {"brier_raw": brier_raw, "brier_calibrated": brier_cal},
    }


def train_sklearn_baseline(
    model_name: str | None = None,
    *,
    feature_bundle: str | None = None,
    target_bundle: str | None = None,
) -> dict[str, Any]:
    settings = _settings()
    resolved_name = model_name or settings.model_name
    trainer = BaselineModelTrainer(model_name=resolved_name)
    store = DataLakeStore()
    dataset = store.read_or_empty("silver/market")
    return trainer.train(dataset) if not dataset.empty else {}


def train_torch_model(
    architecture: str = "mlp",
    *,
    feature_bundle: str | None = None,
    target_bundle: str | None = None,
) -> dict[str, Any]:
    trainer = _build_deep_trainer(architecture=architecture)
    store = DataLakeStore()
    dataset = store.read_or_empty("silver/market")
    return trainer.train(dataset) if not dataset.empty else {}


def train_catboost_model(
    model_name: str = "catboost_classifier",
    *,
    feature_bundle: str | None = None,
    target_bundle: str | None = None,
) -> dict[str, Any]:
    trainer = CatBoostModelTrainer(model_name=model_name)
    store = DataLakeStore()
    dataset = store.read_or_empty("silver/market")
    return trainer.train(dataset) if not dataset.empty else {}
