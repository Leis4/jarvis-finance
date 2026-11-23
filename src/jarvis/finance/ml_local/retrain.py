"""Prevent unattended promotion without an independent feedback evaluation."""
from pathlib import Path
from typing import Any

from jarvis.finance.classification.evaluation import confirmed_examples


def retrain_setfit(db_path: str) -> dict[str, Any]:
    """Report the prerequisite instead of overwriting a production model."""
    count = len(confirmed_examples(Path(db_path)))
    return {"action": "skipped", "reason": f"Confirmed labels: {count}; independent cutoff and candidate version required",
            "confirmed_labels": count, "production_model_changed": False}
