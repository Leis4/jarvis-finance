"""Bounded retrieval evaluation; source-bound quotes are not semantic truth."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from jarvis.knowledge.errors import KnowledgeUnavailable
from jarvis.knowledge.retriever import KnowledgeRetriever


def evaluate_retrieval(retriever: KnowledgeRetriever, cases: list[dict[str, Any]], *,
                       now: datetime, max_age: timedelta, k: int = 3,
                       quote_claims: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Measure frozen page relevance and literal quote support, without generating claims."""
    if now.tzinfo is None or max_age <= timedelta(0) or type(k) is not int or not 1 <= k <= 50 or not 1 <= len(cases) <= 1000:
        raise ValueError("Bounded evaluation and aware freshness policy required")
    seen = set()
    for case in cases:
        query = case.get("query")
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 1000 or query in seen:
            raise ValueError("Unique bounded benchmark questions required")
        seen.add(query)
        for source in case["relevant"]:
            sha = source.get("source_sha256")
            if (not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)
                    or type(source.get("page")) is not int or source["page"] < 1):
                raise ValueError("Explicit source hash/page qrels required")
    if quote_claims is not None and (not set(quote_claims) <= seen or
            any(not isinstance(c, str) or not c.strip() for claims in quote_claims.values() for c in claims)):
        raise ValueError("Quote claims must reference benchmark questions")
    encoded = json.dumps(cases, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    result: dict[str, Any] = {"status": "unavailable", "dataset_sha256": hashlib.sha256(encoded).hexdigest(),
        "k": k, "queries": len(cases), "metrics": None, "now": now.isoformat(),
        "max_age_seconds": max_age.total_seconds(), "model": retriever.store.embedder.model_name,
        "limitations": ["Frozen benchmark is not personal financial quality",
            "Citation correctness is source/page/hash binding, not financial truth",
            "Quote support uses exact text, not entailment or generated-answer evaluation"],
        "database_modified": False, "external_llm_calls": 0}
    recalls, ranks, abstentions = [], [], []
    valid_citations = total_hits = supported = claims_count = 0
    rows = []
    try:
        for case in cases:
            hits = retriever.search(case["query"], top_k=k, max_observation_age=max_age, now=now)
            relevant = {(r["source_sha256"], r["page"]) for r in case["relevant"]}
            retrieved = []
            documents = []
            for hit in hits:
                citation, meta = hit.get("citation", {}), hit["metadata"]
                bound = (meta.get("citation_scope") == "page" and citation.get("page") == meta.get("page")
                    and citation.get("source_sha256") == meta.get("source_sha256")
                    and citation.get("lineage_sha256") == meta.get("lineage_sha256")
                    and citation.get("generation") == meta.get("generation")
                    and meta.get("chunk_sha256") == hashlib.sha256(hit["document"].encode()).hexdigest())
                valid_citations += bound
                total_hits += 1
                if bound:
                    retrieved.append((citation["source_sha256"], citation["page"]))
                    documents.append(hit["document"])
                else:
                    retrieved.append((None, None))
            if relevant:
                recalls.append(len(relevant & set(retrieved)) / len(relevant))
                ranks.append(next((1 / i for i, r in enumerate(retrieved, 1) if r in relevant), 0))
            else:
                abstentions.append(not hits)
            claims = (quote_claims or {}).get(case["query"], [])
            claims_count += len(claims)
            supported += sum(any(claim in doc for doc in documents) for claim in claims)
            rows.append({"query_sha256": hashlib.sha256(case["query"].encode()).hexdigest(),
                         "hits": len(hits), "relevant_pages_retrieved": len(relevant & set(retrieved))})
    except KnowledgeUnavailable as exc:
        result.update(status=getattr(exc, "status", "unavailable"), reason="Backend/model/source evidence unavailable", completed_queries=len(rows))
        return result
    if not total_hits:
        result.update(status="insufficient_data", reason="No retrieved corpus evidence")
        return result
    result.update(status="evaluated", metrics={"recall_at_k": sum(recalls) / len(recalls) if recalls else None,
        "mrr": sum(ranks) / len(ranks) if ranks else None,
        "citation_correctness": valid_citations / total_hits,
        "unsupported_claim_rate": (claims_count - supported) / claims_count if claims_count else None,
        "claim_mode": "literal_quotes_only" if claims_count else "not_evaluated",
        "abstention_accuracy": sum(abstentions) / len(abstentions) if abstentions else None},
        results=rows, answerable_queries=len(recalls), unanswerable_queries=len(abstentions), claims_evaluated=claims_count)
    return result
