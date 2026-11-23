from __future__ import annotations

import json
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.interfaces import ModelTrainer
from jarvis.invest.core.utils import set_global_seed, utc_now
from jarvis.invest.models.metrics import classification_metrics, rank_metrics
from jarvis.invest.models.mlflow_utils import MLflowLogger
from jarvis.invest.models.splits import walk_forward_splits


class BaselineModelTrainer(ModelTrainer):
    def __init__(self, model_name: str | None = None, task: str = "classification") -> None:
        self.settings = get_settings()
        self.model_name = model_name or self.settings.model_name
        self.task = task
        self.output_dir = self.settings.artifacts_dir / "models" / self.model_name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        set_global_seed(self.settings.model_seed)
        self.feature_columns: list[str] = []

    @staticmethod
    def candidate_feature_columns(dataset: pd.DataFrame) -> list[str]:
        excluded = {
            "instrument_id",
            "source_name",
            "source_timestamp",
            "ingestion_timestamp",
            "effective_timestamp",
            "feature_asof_timestamp",
            "data_version",
            "target_outperform",
            "expected_return_h",
            "future_close",
            "benchmark_forward_return_h",
            "peer_median_return_h",
            "sector_median_return_h",
            "absolute_return_threshold_h",
            "forward_rank_h",
            "forward_drawdown_h",
            "downside_dd_threshold_h",
            "regime_trend",
            "regime_mean_reversion",
            "regime_stress",
            "regime_high_vol",
            "timestamp",
        }
        return [c for c in dataset.columns if c not in excluded and pd.api.types.is_numeric_dtype(dataset[c])]

    def _build_estimator(self) -> Any:
        if self.model_name == "logistic_regression":
            return LogisticRegression(max_iter=500, class_weight="balanced", random_state=self.settings.model_seed)
        if self.model_name == "random_forest":
            return RandomForestClassifier(n_estimators=200, max_depth=8, min_samples_leaf=10, random_state=self.settings.model_seed, class_weight="balanced_subsample", n_jobs=-1)
        if self.model_name == "hist_gradient_boosting":
            return HistGradientBoostingClassifier(max_depth=6, learning_rate=0.05, max_iter=250, random_state=self.settings.model_seed)
        raise ValueError(f"Unsupported baseline model: {self.model_name}")

    def train(self, dataset: pd.DataFrame) -> dict[str, object]:
        dataset = dataset.dropna(subset=["target_outperform"]).reset_index(drop=True)
        self.feature_columns = self.candidate_feature_columns(dataset)
        transformer = ColumnTransformer([("num", Pipeline([("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler())]), self.feature_columns)], remainder="drop")
        estimator = self._build_estimator()
        pipeline = Pipeline([("features", transformer), ("estimator", estimator)])
        splits = walk_forward_splits(dataset, n_splits=4, min_train_dates=80)
        oos_frames: list[pd.DataFrame] = []
        cv_metrics: list[dict[str, float]] = []
        logger = MLflowLogger(self.settings.mlflow_tracking_uri)
        with logger.start_run(run_name=f"{self.model_name}_{utc_now().date()}"):
            logger.log_params({"model_family": "baseline", "model_name": self.model_name, "feature_count": len(self.feature_columns)})
            for idx, split in enumerate(splits):
                train_df = dataset.loc[split.train_index]
                test_df = dataset.loc[split.test_index]
                pipeline.fit(train_df[self.feature_columns], train_df["target_outperform"])
                prob = pipeline.predict_proba(test_df[self.feature_columns])[:, 1]
                pred = (prob >= 0.5).astype(int)
                fold_metrics = classification_metrics(test_df["target_outperform"].to_numpy(), prob, pred)
                fold_metrics.update(rank_metrics(test_df.assign(signal_score=prob), score_col="signal_score", target_col="target_outperform", top_k=self.settings.default_top_k))
                cv_metrics.append(fold_metrics)
                logger.log_metrics(fold_metrics, step=idx)
                oos_frames.append(test_df[["instrument_id", "feature_asof_timestamp", "target_outperform", "expected_return_h"]].assign(predicted_probability=prob, predicted_label=pred, fold=idx))
            pipeline.fit(dataset[self.feature_columns], dataset["target_outperform"])
            artifact_path = self.output_dir / "model.joblib"
            metadata_path = self.output_dir / "metadata.json"
            predictions_path = self.output_dir / "cv_predictions.csv"
            joblib.dump({"pipeline": pipeline, "feature_columns": self.feature_columns, "task": self.task, "model_name": self.model_name}, artifact_path)
            cv_predictions = pd.concat(oos_frames, ignore_index=True) if oos_frames else pd.DataFrame()
            cv_predictions.to_csv(predictions_path, index=False)
            aggregate = {key: float(np.mean([fold[key] for fold in cv_metrics])) for key in cv_metrics[0]} if cv_metrics else {}
            base_model = pipeline.named_steps["estimator"]
            importances: dict[str, float] = {}
            if hasattr(base_model, "feature_importances_"):
                importances = dict(zip(self.feature_columns, base_model.feature_importances_.tolist(), strict=False))
            elif hasattr(base_model, "coef_"):
                importances = dict(zip(self.feature_columns, np.abs(base_model.coef_[0]).tolist(), strict=False))
            metadata: dict[str, object] = {"artifact_path": str(artifact_path), "metadata_path": str(metadata_path), "cv_predictions_path": str(predictions_path), "metrics": aggregate, "feature_columns": self.feature_columns, "feature_importance": importances, "model_family": "sklearn", "model_name": self.model_name, "trained_at": utc_now().isoformat()}
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            logger.log_metrics(aggregate)
            logger.log_artifact(str(metadata_path))
            logger.log_artifact(str(predictions_path))
        return metadata
