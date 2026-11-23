"""TransactionRetriever for finding similar past transactions and voting category."""
from __future__ import annotations

from typing import Any

from jarvis.rag.embedder import Embedder
from jarvis.rag.store import VectorStore


class TransactionRetriever:
    """Retrieve similar transactions and vote category."""

    def __init__(self, store: VectorStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder

    def find_similar(
        self,
        merchant: str,
        description: str,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        """Find top-K similar transactions from vector store."""
        query_text = f"{merchant} {description}".strip()
        if not query_text:
            return []
        embedding = self.embedder.encode_one(query_text)
        return self.store.query(query_text, k=k, embedding=embedding)

    def vote_category(self, similar: list[dict[str, Any]]) -> tuple[str, float]:
        """Vote top category from similar items list with distance weighting. Returns (category, confidence)."""
        import math

        if not similar:
            return "uncategorized", 0.0

        scores: dict[str, float] = {}
        total_weight = 0.0
        for item in similar:
            cat = str(item.get("metadata", {}).get("category", "")).strip()
            if cat and cat not in ("uncategorized", "unknown", ""):
                dist = float(item.get("distance", 0.5))
                if dist > 0.65:
                    continue
                weight = math.exp(-2.0 * max(0.0, dist))
                scores[cat] = scores.get(cat, 0.0) + weight
                total_weight += weight

        if not scores or total_weight <= 0:
            return "uncategorized", 0.0

        top_cat, top_score = max(scores.items(), key=lambda x: x[1])
        confidence = round(top_score / total_weight, 2)
        return top_cat, confidence
