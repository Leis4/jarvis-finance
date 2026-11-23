from __future__ import annotations

import json
from typing import cast

import joblib
import numpy as np
import pandas as pd
import torch
from torch import Tensor, nn
from torch.utils.data import DataLoader, TensorDataset

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.interfaces import ModelTrainer
from jarvis.invest.core.utils import set_global_seed, utc_now
from jarvis.invest.models.metrics import classification_metrics, rank_metrics
from jarvis.invest.models.mlflow_utils import MLflowLogger
from jarvis.invest.models.splits import walk_forward_splits


class MLP(nn.Module):
    def __init__(self, input_dim: int, hidden_dim: int = 128) -> None:
        super().__init__()
        self.net = nn.Sequential(nn.Linear(input_dim, hidden_dim), nn.ReLU(), nn.Dropout(0.2), nn.Linear(hidden_dim, hidden_dim // 2), nn.ReLU(), nn.Linear(hidden_dim // 2, 1))

    def forward(self, x: Tensor) -> Tensor:
        return cast(Tensor, self.net(x).squeeze(-1))


class SequenceGRU(nn.Module):
    def __init__(self, input_dim: int, hidden_dim: int = 64) -> None:
        super().__init__()
        self.gru = nn.GRU(input_dim, hidden_dim, batch_first=True)
        self.head = nn.Linear(hidden_dim, 1)

    def forward(self, x: Tensor) -> Tensor:
        output, _ = self.gru(x)
        return cast(Tensor, self.head(output[:, -1, :]).squeeze(-1))


class TemporalCNN(nn.Module):
    def __init__(self, input_dim: int, seq_len: int) -> None:
        super().__init__()
        self.net = nn.Sequential(nn.Conv1d(input_dim, 32, kernel_size=3, padding=1), nn.ReLU(), nn.Conv1d(32, 16, kernel_size=3, padding=1), nn.ReLU(), nn.AdaptiveAvgPool1d(1))
        self.head = nn.Linear(16, 1)

    def forward(self, x: Tensor) -> Tensor:
        latent = self.net(x.transpose(1, 2)).squeeze(-1)
        return cast(Tensor, self.head(latent).squeeze(-1))


class TransformerTabular(nn.Module):
    def __init__(self, input_dim: int, n_heads: int = 4, hidden_dim: int = 64) -> None:
        super().__init__()
        self.proj = nn.Linear(input_dim, hidden_dim)
        layer = nn.TransformerEncoderLayer(d_model=hidden_dim, nhead=n_heads, batch_first=True)
        self.encoder = nn.TransformerEncoder(layer, num_layers=2)
        self.head = nn.Linear(hidden_dim, 1)

    def forward(self, x: Tensor) -> Tensor:
        encoded = self.encoder(self.proj(x))
        return cast(Tensor, self.head(encoded[:, -1, :]).squeeze(-1))


class DeepModelTrainer(ModelTrainer):
    def __init__(self, architecture: str = "mlp", epochs: int = 12, batch_size: int = 64) -> None:
        self.settings = get_settings()
        self.architecture = architecture
        self.epochs = epochs
        self.batch_size = batch_size
        self.device = torch.device("cpu")
        self.output_dir = self.settings.artifacts_dir / "models" / architecture
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _feature_columns(dataset: pd.DataFrame) -> list[str]:
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
            "timestamp",
        }
        return [c for c in dataset.columns if c not in excluded and pd.api.types.is_numeric_dtype(dataset[c])]

    def _build_model(self, input_dim: int) -> nn.Module:
        if self.architecture == "mlp":
            return MLP(input_dim)
        if self.architecture in {"lstm", "gru"}:
            return SequenceGRU(input_dim)
        if self.architecture in {"cnn", "tcn"}:
            return TemporalCNN(input_dim, seq_len=1)
        if self.architecture == "transformer":
            return TransformerTabular(input_dim)
        raise ValueError(f"Unsupported deep architecture: {self.architecture}")

    def train(self, dataset: pd.DataFrame) -> dict[str, object]:
        set_global_seed(self.settings.model_seed)
        dataset = dataset.dropna(subset=["target_outperform"]).reset_index(drop=True)
        feature_columns = self._feature_columns(dataset)
        splits = walk_forward_splits(dataset, n_splits=3, min_train_dates=80)
        logger = MLflowLogger(self.settings.mlflow_tracking_uri)
        cv_metrics: list[dict[str, float]] = []
        oos_frames: list[pd.DataFrame] = []
        with logger.start_run(run_name=f"{self.architecture}_{utc_now().date()}"):
            logger.log_params({"model_family": "deep", "architecture": self.architecture})
            for idx, split in enumerate(splits):
                train_df = dataset.loc[split.train_index]
                test_df = dataset.loc[split.test_index]
                x_train = train_df[feature_columns].fillna(0.0).to_numpy(dtype=np.float32)
                y_train = train_df["target_outperform"].to_numpy(dtype=np.float32)
                x_test = test_df[feature_columns].fillna(0.0).to_numpy(dtype=np.float32)
                y_test = test_df["target_outperform"].to_numpy(dtype=np.float32)
                x_train_t = torch.tensor(x_train)
                x_test_t = torch.tensor(x_test)
                if self.architecture != "mlp":
                    x_train_t = x_train_t.unsqueeze(1)
                    x_test_t = x_test_t.unsqueeze(1)
                train_ds = TensorDataset(x_train_t, torch.tensor(y_train))
                loader = DataLoader(train_ds, batch_size=self.batch_size, shuffle=True)
                model = self._build_model(len(feature_columns)).to(self.device)
                optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
                criterion = nn.BCEWithLogitsLoss()
                for _ in range(self.epochs):
                    model.train()
                    for batch_x, batch_y in loader:
                        optimizer.zero_grad(set_to_none=True)
                        loss = criterion(model(batch_x), batch_y)
                        loss.backward()
                        optimizer.step()
                model.eval()
                with torch.no_grad():
                    prob = torch.sigmoid(model(x_test_t)).cpu().numpy()
                pred = (prob >= 0.5).astype(int)
                fold_metrics = classification_metrics(y_test.astype(int), prob, pred)
                fold_metrics.update(rank_metrics(test_df.assign(signal_score=prob), score_col="signal_score", target_col="target_outperform", top_k=self.settings.default_top_k))
                cv_metrics.append(fold_metrics)
                logger.log_metrics(fold_metrics, step=idx)
                oos_frames.append(
                    test_df[["instrument_id", "feature_asof_timestamp", "target_outperform", "expected_return_h"]]
                    .assign(predicted_probability=prob, predicted_label=pred, fold=idx)
                )
            full_x = dataset[feature_columns].fillna(0.0).to_numpy(dtype=np.float32)
            full_y = dataset["target_outperform"].to_numpy(dtype=np.float32)
            full_t = torch.tensor(full_x)
            if self.architecture != "mlp":
                full_t = full_t.unsqueeze(1)
            full_ds = TensorDataset(full_t, torch.tensor(full_y))
            loader = DataLoader(full_ds, batch_size=self.batch_size, shuffle=True)
            model = self._build_model(len(feature_columns))
            optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
            criterion = nn.BCEWithLogitsLoss()
            for _ in range(self.epochs):
                for batch_x, batch_y in loader:
                    optimizer.zero_grad(set_to_none=True)
                    loss = criterion(model(batch_x), batch_y)
                    loss.backward()
                    optimizer.step()
            model_path = self.output_dir / "model.pt"
            feature_columns_path = self.output_dir / "feature_columns.joblib"
            metadata_path = self.output_dir / "metadata.json"
            predictions_path = self.output_dir / "cv_predictions.csv"
            torch.save(model.state_dict(), model_path)
            joblib.dump(feature_columns, feature_columns_path)
            cv_predictions = pd.concat(oos_frames, ignore_index=True) if oos_frames else pd.DataFrame()
            cv_predictions.to_csv(predictions_path, index=False)
            metadata: dict[str, object] = {"model_path": str(model_path), "feature_columns_path": str(feature_columns_path), "metadata_path": str(metadata_path), "cv_predictions_path": str(predictions_path), "feature_columns": feature_columns, "architecture": self.architecture, "metrics": {key: float(np.mean([fold[key] for fold in cv_metrics])) for key in cv_metrics[0]} if cv_metrics else {}, "model_family": "torch", "model_name": self.architecture, "trained_at": utc_now().isoformat()}
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            logger.log_artifact(str(metadata_path))
            logger.log_artifact(str(predictions_path))
        return metadata
