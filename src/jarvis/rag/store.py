"""Persistent transaction retrieval with explicit backend failures."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class VectorStore:
    """Keep retrieval failures separate from an empty collection."""

    def __init__(self, persist_dir: str = "data/rag", embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2") -> None:
        self.persist_dir = persist_dir
        self.embedding_model = embedding_model
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self._client: Any = None
        self._collection: Any = None

    def _ensure_client(self) -> None:
        if self._collection is not None:
            return
        try:
            import chromadb  # type: ignore[import-not-found]
            self._client = chromadb.PersistentClient(path=self.persist_dir)
            collection = self._client.get_or_create_collection(
                name="transactions_labeled", embedding_function=None,
                metadata={"embedding_model": self.embedding_model, "schema_version": 1})
            if (collection.metadata or {}).get("embedding_model") != self.embedding_model:
                raise ValueError("Legacy or incompatible embedding index; build a separate verified version")
            self._collection = collection
        except Exception as exc:
            self._client = None
            raise RuntimeError("Vector backend unavailable") from exc

    def add(self, id: str, text: str, metadata: dict[str, Any],
            embedding: list[float] | None = None) -> None:
        self._ensure_client()
        if embedding is None:
            raise ValueError("Explicit embedding from the declared model is required")
        kwargs: dict[str, Any] = {"ids": [id], "documents": [text], "metadatas": [metadata]}
        if embedding is not None:
            kwargs["embeddings"] = [embedding]
        try:
            self._collection.add(**kwargs)
        except Exception as exc:
            raise RuntimeError("Vector write failed") from exc

    def query(self, text: str, k: int = 10,
              embedding: list[float] | None = None) -> list[dict[str, Any]]:
        self._ensure_client()
        if embedding is None:
            raise ValueError("Explicit embedding from the declared model is required")
        if k < 1:
            raise ValueError("k must be positive")
        count = self.count()
        if count == 0:
            return []
        kwargs: dict[str, Any] = {"n_results": min(k, count)}
        if embedding is not None:
            kwargs["query_embeddings"] = [embedding]
        else:
            kwargs["query_texts"] = [text]
        try:
            result = self._collection.query(**kwargs)
            ids, docs, metas, distances = (result[key][0] for key in
                                           ("ids", "documents", "metadatas", "distances"))
            if not (len(ids) == len(docs) == len(metas) == len(distances)):
                raise ValueError("Incomplete retrieval response")
            return [{"id": ident, "text": doc, "metadata": meta, "distance": distance}
                    for ident, doc, meta, distance in zip(ids, docs, metas, distances, strict=True)]
        except Exception as exc:
            raise RuntimeError("Vector query failed") from exc

    def count(self) -> int:
        self._ensure_client()
        try:
            return int(self._collection.count())
        except Exception as exc:
            raise RuntimeError("Vector count failed") from exc

    def reset(self) -> None:
        self._ensure_client()
        try:
            self._client.delete_collection("transactions_labeled")
            self._collection = None
            self._ensure_client()
        except Exception as exc:
            raise RuntimeError("Vector reset failed") from exc
