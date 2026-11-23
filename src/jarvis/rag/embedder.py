"""Sentence-transformer embeddings with explicit availability errors."""
from __future__ import annotations

from typing import Any


class Embedder:
    """Load the requested embedding model lazily."""

    def __init__(self, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2") -> None:
        self.model_name = model_name
        self._model: Any = None

    def _load_model(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import (
                SentenceTransformer,  # type: ignore[import-not-found]
            )
            self._model = SentenceTransformer(self.model_name)
        except Exception as exc:
            raise RuntimeError("Embedding model unavailable; retrieval cannot be evaluated") from exc

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode text without substituting another embedding space."""
        self._load_model()
        try:
            result = self._model.encode(texts, convert_to_numpy=True)
            return [row.tolist() for row in result]
        except Exception as exc:
            raise RuntimeError("Embedding inference failed") from exc

    def encode_one(self, text: str) -> list[float]:
        return self.encode([text])[0]
