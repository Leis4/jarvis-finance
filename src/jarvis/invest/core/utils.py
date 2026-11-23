from __future__ import annotations

import hashlib
import json
import os
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np


def utc_now() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(UTC)


def ensure_dir(path: Path) -> Path:
    """Create a directory when missing and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def stable_hash(payload: dict[str, Any]) -> str:
    """Build a deterministic digest for idempotency keys."""
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def set_global_seed(seed: int) -> None:
    """Seed Python, NumPy and hash randomization."""
    random.seed(seed)
    np.random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
