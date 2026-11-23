"""Local SetFit categorizer with confidence threshold."""
import hashlib
import json
import logging
import stat
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from setfit import SetFitModel  # type: ignore[import-not-found,import-untyped]
except ImportError:
    SetFitModel = None

logger = logging.getLogger(__name__)

MODEL_DIR = Path("models/setfit_v1")
CONFIDENCE_THRESHOLD = 0.70
TRANSFER_KEYWORDS = ("перевод", "sbol", "sberbank onl@in", "alfa-bank", "ozon bank", "t-bank")


class LocalCategorizer:
    """SetFit-based local categorizer with lazy loading."""
    
    def __init__(self, model_dir: Path | None = None, threshold: float = CONFIDENCE_THRESHOLD):
        self._default_path = model_dir is None
        self.model_dir = model_dir if model_dir is not None else MODEL_DIR
        self.threshold = threshold
        self._model: Any = None
        self._labels: list[str] | None = None

    @property
    def is_available(self) -> bool:
        """Return True if model directory exists and SetFit is installed."""
        if SetFitModel is None:
            return False
        try:
            alt_dir = Path("artifacts/models/setfit_classifier")
            if self._default_path and alt_dir.exists():
                self.model_dir = alt_dir
                return True
            return self.model_dir.exists()
        except Exception:  # noqa: BLE001
            return False

    def artifact_evidence(self, expected_sha256: str | None = None) -> dict[str, Any]:
        """Observe actual local bytes/metadata; hashes cannot authorize pickle loading."""
        root = self.model_dir.absolute()
        required = {"config.json", "config_setfit.json", "modules.json", "model.safetensors", "model_head.pkl"}
        try:
            for ancestor in [root, *root.parents]:
                info = ancestor.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Reparse model path refused")
            if not root.is_dir() or not required.issubset({entry.name for entry in root.iterdir()}):
                return {"status": "unavailable", "reason": "complete local trained artifact required", "artifact_sha256": None, "inference_allowed": False}
            inventory: dict[str, str] = {}
            pending = [root]
            total = 0
            while pending:
                folder = pending.pop()
                for entry in folder.iterdir():
                    before = entry.lstat()
                    if stat.S_ISLNK(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400 or before.st_nlink > 1 and entry.is_file():
                        raise ValueError("Linked model member refused")
                    if stat.S_ISDIR(before.st_mode):
                        if len(entry.relative_to(root).parts) > 4:
                            raise ValueError("Bounded model nesting required")
                        pending.append(entry)
                        continue
                    if not stat.S_ISREG(before.st_mode) or entry.suffix not in {".json", ".txt", ".safetensors", ".pkl", ".model", ".vocab"}:
                        raise ValueError("Unexpected model member")
                    total += before.st_size
                    if total > 2_000_000_000 or len(inventory) >= 100:
                        raise ValueError("Bounded model bytes/members required")
                    digest = hashlib.sha256()
                    with entry.open("rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                    after = entry.lstat()
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                        raise ValueError("Model changed during observation")
                    inventory[entry.relative_to(root).as_posix()] = digest.hexdigest()
            config_path = root / "config_setfit.json"
            if config_path.stat().st_size > 100_000:
                raise ValueError("Bounded model metadata required")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            labels = config.get("labels")
            if not isinstance(labels, list) or len(labels) < 2 or len(labels) > 100 or any(not isinstance(label, str) or not label for label in labels):
                raise ValueError("Trained label metadata required")
            payload = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
            actual = hashlib.sha256(payload).hexdigest()
            return {"status": "blocked", "reason": "serialized head source/trust and runtime compatibility unverified", "artifact_sha256": actual,
                    "expected_digest_matches": expected_sha256 is not None and actual == expected_sha256,
                    "metadata_shape_valid": True, "runtime_compatible": None, "member_count": len(inventory), "total_bytes": total,
                    "inference_allowed": False, "weights_loaded": False, "filesystem_isolation": "unverified"}
        except (ValueError, OSError, TypeError):
            return {"status": "unavailable", "reason": "local artifact validation failed", "artifact_sha256": None, "inference_allowed": False}

    @property
    def confidence_threshold(self) -> float:
        return self.threshold

    def get_labels(self) -> list[str]:
        """Return list of model labels."""
        try:
            self._ensure_loaded()
            return self._labels or []
        except Exception:  # noqa: BLE001
            return []

    def _ensure_loaded(self) -> None:
        if SetFitModel is None:
            raise RuntimeError("setfit not installed")
        if self._model is not None:
            return
        if not self.model_dir.exists():
            raise FileNotFoundError(f"SetFit model not found: {self.model_dir}")
        logger.info(f"Loading SetFit from {self.model_dir}")
        self._model = SetFitModel.from_pretrained(str(self.model_dir))
        self._labels = list(self._model.labels)
        logger.info(f"SetFit loaded with {len(self._labels or [])} labels")
    
    def predict(self, text: str) -> dict[str, Any] | None:
        """Predict category. Returns None if confidence < threshold."""
        if not text or len(text.strip()) < 3:
            return None
        
        # Reject transfer-like texts
        low = text.lower()
        if any(kw in low for kw in TRANSFER_KEYWORDS):
            logger.debug(f"Rejecting transfer text: {text[:50]}")
            return None
        
        try:
            self._ensure_loaded()
        except Exception:  # noqa: BLE001 - loading failure is not abstention
            raise RuntimeError("SetFit model unavailable") from None
        
        probs = self._model.predict_proba([text])[0]
        # Convert to numpy if torch tensor
        try:
            probs = probs.cpu().numpy()
        except AttributeError:
            pass
        
        top_idx = probs.argsort()[::-1][:3]
        top_conf = float(probs[top_idx[0]])
        
        labels = self._labels or []
        result = {
            "category": labels[top_idx[0]],
            "confidence": top_conf,
            "top3": [
                {"label": labels[j], "prob": float(probs[j])}
                for j in top_idx
            ],
            "source": "setfit",
        }
        
        if top_conf < self.threshold:
            logger.debug(f"SetFit confidence {top_conf:.2f} < {self.threshold}, fallback needed")
            return None
        
        return result
    
    def predict_anyway(self, text: str) -> dict[str, Any] | None:
        """Predict regardless of confidence (for diagnostics)."""
        if not text:
            return None
        self._ensure_loaded()
        probs = self._model.predict_proba([text])[0]
        try:
            probs = probs.cpu().numpy()
        except AttributeError:
            pass
        top_idx = probs.argsort()[::-1][:3]
        labels = self._labels or []
        return {
            "category": labels[top_idx[0]],
            "confidence": float(probs[top_idx[0]]),
            "top3": [
                {"label": labels[j], "prob": float(probs[j])}
                for j in top_idx
            ],
            "source": "setfit",
        }


@lru_cache(maxsize=1)
def get_local_categorizer() -> LocalCategorizer:
    return LocalCategorizer()
