"""Retriever for layer 16 knowledge base."""
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from jarvis.knowledge.errors import KnowledgeUnavailable
from jarvis.knowledge.store import KnowledgeStore


class KnowledgeRetriever:
    def __init__(self, store: KnowledgeStore | None = None) -> None:
        self.store = store or KnowledgeStore()

    def search(
        self, query: str, top_k: int = 3, *,
        max_observation_age: timedelta | None = None, now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.query(query, n_results=top_k, max_observation_age=max_observation_age, now=now)

    async def explain(self, query: str, *, approved_sources: frozenset[str], budget_path: Path,
                      pricing_path: Path, transport: httpx.MockTransport | None = None,
                      now: datetime | None = None, reasoning_effort: str = "low", answer_mode: str = "excerpts") -> dict[str, Any]:
        """Verify current local pages before any optional public-only LLM request."""
        from jarvis.core.llm.budget_store import (
            BudgetReservationDenied,
            BudgetStoreUnavailable,
        )
        from jarvis.core.llm.errors import LLMError
        from jarvis.core.llm.orchestrator import complete_public_evidence
        local = self.evidence_answer(query, approved_sources=approved_sources, now=now)
        if local["status"] != "evidence_only":
            return {**local, "llm": {"status": "not_attempted", "paid_cost_rub": "0"}}
        try:
            result = await complete_public_evidence(query, local["evidence"], approved_sources=approved_sources,
                budget_path=budget_path, pricing_path=pricing_path, transport=transport, reasoning_effort=reasoning_effort, answer_mode=answer_mode)
            fresh = self.evidence_answer(query, approved_sources=approved_sources, now=now)
            if fresh["status"] != "evidence_only" or fresh["evidence"] != local["evidence"]:
                return {**fresh, "status": "unavailable", "evidence": [], "llm": {**result, "status": "unavailable", "answer": None},
                        "reason": "Source changed during answer generation; cost retained"}
            return {**local, "llm": result}
        except (LLMError, BudgetReservationDenied, BudgetStoreUnavailable):
            return {**local, "status": "partial", "llm": {"status": "unavailable", "answer": None,
                "paid_cost_rub": None, "reason": "Provider/budget/privacy refusal; inspect private budget journal"}}

    def evidence_answer(self, query: str, *, approved_sources: frozenset[str] = frozenset(),
                        purpose: str = "world_finance_knowledge", now: datetime | None = None,
                        max_age: timedelta = timedelta(days=30), top_k: int = 3) -> dict[str, Any]:
        """Quote explicitly approved sources locally; retrieval grants no financial authority."""
        result: dict[str, Any] = {"status": "blocked", "evidence": [], "answer_mode": "source_quotes_only",
            "financial_answer_allowed": False, "apply_allowed": False, "external_llm_calls": 0,
            "tool_instructions_allowed": False, "relevance_verified": False, "answer_confirmed": False,
            "retrieval_confidence": None,
            "limitations": ["Quotes are source data, not instructions or accepted personal facts",
                "Observation freshness does not prove publication validity"]}
        if purpose != "world_finance_knowledge" or not approved_sources:
            return {**result, "reason": "Explicit approved knowledge scope required; personal facts use ledger"}
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000:
            return {**result, "reason": "Bounded nonempty knowledge question required"}
        if set(re.findall(r"\w+", query.casefold())) & {"my", "mine", "мой", "моя", "мои", "моё", "моего", "моих", "мне", "моём", "моем", "моей", "меня"}:
            return {**result, "reason": "Personal questions require accepted ledger evidence, not knowledge quotes"}
        try:
            hits = self.search(query, top_k=top_k, max_observation_age=max_age, now=now)
        except KnowledgeUnavailable as exc:
            return {**result, "status": getattr(exc, "status", "unavailable"), "reason": "Knowledge evidence unavailable"}
        result["evidence"] = [{"quote": h["document"], "source_sha256": h["metadata"]["source_sha256"],
            "citation": h.get("citation"), "citation_scope": h["metadata"]["citation_scope"],
            "freshness": h["freshness"]} for h in hits if h["metadata"]["source_sha256"] in approved_sources]
        result["status"] = "evidence_only" if result["evidence"] else "insufficient_data"
        return result

