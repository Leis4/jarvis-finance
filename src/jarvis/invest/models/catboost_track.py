from __future__ import annotations

import json
from typing import Any, cast

import joblib
import numpy as np
import pandas as pd

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.interfaces import ModelTrainer
from jarvis.invest.core.utils import set_global_seed, utc_now
from jarvis.invest.models.metrics import classification_metrics, rank_metrics
from jarvis.invest.models.mlflow_utils import MLflowLogger
from jarvis.invest.models.splits import walk_forward_splits


class CatBoostModelTrainer(ModelTrainer):
    def __init__(self, model_name: str = "catboost_classifier") -> None:
        self.settings = get_settings()
        self.model_name = model_name
        self.output_dir = self.settings.artifacts_dir / "models" / self.model_name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        set_global_seed(self.settings.model_seed)
        self.feature_columns: list[str] = []
        self.categorical_columns: list[str] = []

    @staticmethod
    def _excluded_columns() -> set[str]:
        return {
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
            "timestamp",
        }

    def _resolve_feature_contract(self, dataset: pd.DataFrame) -> tuple[list[str], list[str]]:
        excluded = self._excluded_columns()
        allowed_categorical = {"asset_class", "sector_name", "board_id", "currency"}
        feature_columns = [c for c in dataset.columns if c not in excluded and (pd.api.types.is_numeric_dtype(dataset[c]) or c in allowed_categorical)]
        categorical = [c for c in feature_columns if c in allowed_categorical and c in dataset.columns]
        return feature_columns, categorical

    def train(self, dataset: pd.DataFrame) -> dict[str, object]:
        dataset = dataset.dropna(subset=["target_outperform"]).reset_index(drop=True)
        self.feature_columns, self.categorical_columns = self._resolve_feature_contract(dataset)
        if not self.feature_columns:
            raise ValueError("CatBoost training contract is empty: no usable feature columns.")
        try:
            from catboost import CatBoostClassifier, Pool
        except Exception as exc:
            raise RuntimeError("CatBoost не установлен в окружении. Установите пакет catboost для optional booster track.") from exc

        def _prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
            prepared = frame[self.feature_columns].copy()
            for col in self.categorical_columns:
                prepared[col] = prepared[col].astype(str).fillna("UNKNOWN")
            for col in self.feature_columns:
                if col in self.categorical_columns:
                    continue
                prepared[col] = pd.to_numeric(prepared[col], errors="coerce")
            return prepared

        params = {
            "loss_function": "Logloss",
            "eval_metric": "AUC",
            "iterations": 350,
            "learning_rate": 0.05,
            "depth": 6,
            "random_seed": self.settings.model_seed,
            "verbose": False,
            "allow_writing_files": False,
        }
        splits = walk_forward_splits(dataset, n_splits=4, min_train_dates=80, gap_dates=1, embargo_dates=1)
        oos_frames: list[pd.DataFrame] = []
        cv_metrics: list[dict[str, float]] = []
        best_iterations: list[int] = []
        logger = MLflowLogger(self.settings.mlflow_tracking_uri)
        with logger.start_run(run_name=f"{self.model_name}_{utc_now().date()}"):
            logger.log_params(
                {
                    "model_family": "catboost",
                    "model_name": self.model_name,
                    "feature_count": len(self.feature_columns),
                    "categorical_count": len(self.categorical_columns),
                    "categorical_columns": ",".join(self.categorical_columns),
                }
            )
            for idx, split in enumerate(splits):
                train_df = dataset.loc[split.train_index]
                test_df = dataset.loc[split.test_index]
                x_train = _prepare_frame(train_df)
                x_test = _prepare_frame(test_df)
                cat_idx = [self.feature_columns.index(c) for c in self.categorical_columns]
                train_pool = Pool(x_train, label=train_df["target_outperform"].astype(int), cat_features=cat_idx)
                test_pool = Pool(x_test, label=test_df["target_outperform"].astype(int), cat_features=cat_idx)
                model = CatBoostClassifier(**params)
                model.fit(train_pool, eval_set=test_pool, use_best_model=True, early_stopping_rounds=30)
                best_iterations.append(int(cast(Any, model.get_best_iteration() or params["iterations"])))
                prob = model.predict_proba(x_test)[:, 1]
                pred = (prob >= 0.5).astype(int)
                fold_metrics = classification_metrics(test_df["target_outperform"].to_numpy(), prob, pred)
                fold_metrics.update(rank_metrics(test_df.assign(signal_score=prob), score_col="signal_score", target_col="target_outperform", top_k=self.settings.default_top_k))
                cv_metrics.append(fold_metrics)
                logger.log_metrics(fold_metrics, step=idx)
                oos_frames.append(
                    test_df[["instrument_id", "feature_asof_timestamp", "target_outperform", "expected_return_h"]].assign(
                        predicted_probability=prob, predicted_label=pred, fold=idx
                    )
                )

            final_iterations = int(cast(Any, np.median(best_iterations))) if best_iterations else int(cast(Any, params["iterations"]))
            final_params = {**params, "iterations": max(50, final_iterations)}
            final_model = CatBoostClassifier(**final_params)
            x_full = _prepare_frame(dataset)
            cat_idx = [self.feature_columns.index(c) for c in self.categorical_columns]
            final_pool = Pool(x_full, label=dataset["target_outperform"].astype(int), cat_features=cat_idx)
            final_model.fit(final_pool, verbose=False)

            artifact_path = self.output_dir / "model.joblib"
            metadata_path = self.output_dir / "metadata.json"
            predictions_path = self.output_dir / "cv_predictions.csv"
            joblib.dump(
                {
                    "pipeline": final_model,
                    "feature_columns": self.feature_columns,
                    "categorical_columns": self.categorical_columns,
                    "task": "classification",
                    "model_name": self.model_name,
                },
                artifact_path,
            )
            cv_predictions = pd.concat(oos_frames, ignore_index=True) if oos_frames else pd.DataFrame()
            cv_predictions.to_csv(predictions_path, index=False)
            aggregate = {key: float(np.mean([fold[key] for fold in cv_metrics])) for key in cv_metrics[0]} if cv_metrics else {}
            importances = dict(zip(self.feature_columns, final_model.get_feature_importance().tolist(), strict=False))
            metadata = {
                "artifact_path": str(artifact_path),
                "metadata_path": str(metadata_path),
                "cv_predictions_path": str(predictions_path),
                "metrics": aggregate,
                "feature_columns": self.feature_columns,
                "categorical_columns": self.categorical_columns,
                "feature_importance": importances,
                "model_family": "catboost",
                "model_name": self.model_name,
                "trained_at": utc_now().isoformat(),
                "best_iteration_cv_median": max(50, final_iterations),
            }
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            logger.log_metrics(aggregate)
            logger.log_artifact(str(metadata_path))
            logger.log_artifact(str(predictions_path))
        return metadata
