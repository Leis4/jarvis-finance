"""Independent time and merchant evaluation from confirmed human labels."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Example:
    transaction_id: int
    day: date
    merchant: str
    text: str
    label: str


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def confirmed_examples(db_path: Path) -> list[Example]:
    """Read only the latest manual correction; bank labels are not ground truth."""
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='category_corrections'").fetchone():
            return []
        rows = conn.execute("""
            SELECT t.id, t.tx_date, t.merchant, t.description, c.new_category
            FROM transactions t JOIN category_corrections c ON c.transaction_id=t.id
            WHERE c.source='manual' AND c.new_category=t.category AND c.version=(SELECT MAX(version)
              FROM category_corrections WHERE transaction_id=t.id)
            ORDER BY t.tx_date,t.id
        """).fetchall()
    return [Example(int(ident), date.fromisoformat(str(day)[:10]), normalize(merchant or ""),
                    normalize(f"{merchant or ''} {description or ''}"), str(label))
            for ident, day, merchant, description, label in rows
            if str(label) not in {"", "unknown", "uncategorized", "pending_review"} and (merchant or description)]


def independent_split(examples: Sequence[Example], cutoff: date) -> tuple[list[Example], list[Example], dict[str, int]]:
    """Exclude ambiguous texts and later merchants from all training data."""
    text_labels: dict[str, set[str]] = {}
    for row in examples:
        text_labels.setdefault(row.text, set()).add(row.label)
    conflicts = {text for text, labels in text_labels.items() if len(labels) > 1}
    seen: set[str] = set()
    clean: list[Example] = []
    for row in sorted(examples, key=lambda item: (item.day, item.transaction_id), reverse=True):
        if row.text not in conflicts and row.text not in seen:
            clean.append(row)
            seen.add(row.text)
    test = sorted([row for row in clean if row.day >= cutoff], key=lambda row: row.day)
    test_merchants = {row.merchant for row in test}
    train = sorted([row for row in clean if row.day < cutoff and row.merchant
                    and row.merchant not in test_merchants], key=lambda row: row.day)
    return train, test, {"input": len(examples), "conflicting_texts": len(conflicts),
                         "deduplicated": len(clean), "excluded": len(examples) - len(train) - len(test)}


def metrics(truth: Sequence[str], predictions: Sequence[str | None]) -> dict[str, Any]:
    """Count abstentions and unseen classes in the complete holdout."""
    if not truth or len(truth) != len(predictions):
        raise ValueError("A nonempty holdout and one prediction per row are required")
    from sklearn.metrics import (  # type: ignore[import-untyped]
        confusion_matrix,
        f1_score,
        precision_recall_fscore_support,
    )

    labels = sorted(set(truth))
    predicted = [value if value is not None else "__abstain__" for value in predictions]
    precision, recall, f1, support = precision_recall_fscore_support(list(truth), predicted, labels=labels, zero_division=0)
    return {"count": len(truth), "coverage": sum(value is not None for value in predictions) / len(truth),
            "accuracy": sum(a == b for a, b in zip(truth, predicted, strict=True)) / len(truth),
            "macro_f1": float(f1_score(list(truth), predicted, labels=labels, average="macro", zero_division=0)),
            "confusion_labels": sorted(set(truth)|set(predicted)),
            "confusion_matrix": confusion_matrix(list(truth),predicted,labels=sorted(set(truth)|set(predicted))).tolist(),
            "class_counts": dict(Counter(truth)),
            "per_category": {label: {"precision": float(precision[i]), "recall": float(recall[i]),
                "f1": float(f1[i]), "support": int(support[i])} for i,label in enumerate(labels)},
            "confidence_calibration": "not_measured_no_probabilistic_scores"}


def evaluate(examples: Sequence[Example], cutoff: date,
             fit_predict: Callable[[list[Example], list[Example]], Sequence[str | None]],
             rules: Callable[[str], str | None]) -> dict[str, Any]:
    train, test, excluded = independent_split(examples, cutoff)
    fingerprint = hashlib.sha256(json.dumps([asdict(row) for row in examples],
                                            default=str, sort_keys=True).encode()).hexdigest()
    report: dict[str, Any] = {"label_source": "latest_manual_category_correction", "data_sha256": fingerprint,
                             "cutoff": cutoff.isoformat(), "seed": 42, "train_size": len(train),
                             "test_size": len(test), "split": "time_and_merchant_disjoint", "exclusions": excluded,
                             "train_ids": [row.transaction_id for row in train],
                             "test_ids": [row.transaction_id for row in test]}
    if not train or not test:
        return {**report, "status": "blocked", "reason": "No independent confirmed train/test labels", "metrics": None}
    truth = [row.label for row in test]
    majority = Counter(row.label for row in train).most_common(1)[0][0]
    report.update(status="evaluated", metrics=metrics(truth, fit_predict(train, test)),
                  majority_baseline=metrics(truth, [majority] * len(test)),
                  rules_baseline=metrics(truth, [rules(row.text) for row in test]),
                  unseen_test_classes=sorted(set(truth) - {row.label for row in train}))
    return report
