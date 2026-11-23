"""Combine rule-first suggestions without creating human confirmations."""
from collections.abc import Callable
from functools import partial
from typing import Any

from jarvis.finance.classification.layers import (
    LayerResult,
    l0_preprocess,
    l1_hard_rules,
    l2_learned_patterns,
    l3_historical_vote,
    l4_temporal,
    l5_merchant_kb,
    l6_statistical,
    l7_amount_signature,
    l8_5_rag_vote,
    l8_setfit,
    l9_llm_context,
)
from jarvis.finance.event_taxonomy import TAXONOMY_VERSION, canonical_category


async def classify_transaction(
    db_path: str, desc: str, amount: float, category: str = "",
) -> dict[str, Any]:
    """
    Run all layers. Returns {
        category: str,
        confidence: float,
        reasoning: list[str],
        escalate: bool,
        layers_run: int,
    }
    """
    ctx = l0_preprocess(desc, amount, category=category)
    results: list[LayerResult] = []
    states: dict[str, dict[str, str]] = {}

    def record(name: str, result: LayerResult | None) -> LayerResult | None:
        states[name] = {"status": "matched" if result else "no_match"}
        return result

    def run(name: str, fn: Callable[[], LayerResult | None]) -> LayerResult | None:
        try:
            return record(name, fn())
        except Exception as exc:  # noqa: BLE001 - optional classifier boundary
            states[name] = {"status": "unavailable", "error_type": type(exc).__name__}
            return None

    def evidence(result: dict[str, Any]) -> dict[str, Any]:
        result["category"] = canonical_category(result["category"])
        result.update(layer_status=states, human_confirmed=False, taxonomy_version=TAXONOMY_VERSION)
        if states.get('L9_llm_context', {}).get('status') == 'matched':
            result.update(escalate=True, confidence_kind='heuristic_uncalibrated', ai_review_required=True)
        return result

    def immediate(result: LayerResult) -> dict[str, Any]:
        return evidence({"category": result.category, "confidence": result.confidence,
                         "reasoning": [f"{result.layer}: {result.reasoning}"],
                         "escalate": False, "layers_run": 1})

    hard = run("L1_hard_rules", lambda: l1_hard_rules(db_path, ctx))
    if hard:
        return immediate(hard)
    merchant = run("L5_merchant_kb", lambda: l5_merchant_kb(ctx))
    if merchant and merchant.confidence >= 0.9:
        return immediate(merchant)
    if merchant:
        results.append(merchant)

    # Synchronous layers (fast)
    for name, db_fn in (("L2_learned_patterns", l2_learned_patterns), ("L3_historical_vote", l3_historical_vote),
                       ("L4_temporal", l4_temporal), ("L6_statistical", l6_statistical), ("L8_setfit", l8_setfit)):
        r = run(name, partial(db_fn, db_path, ctx))
        if r:
            results.append(r)

    for name, fn in (("L8_5_rag_vote", l8_5_rag_vote), ("L7_amount_signature", l7_amount_signature)):
        r = run(name, partial(fn, ctx))
        if r:
            results.append(r)

    # If no strong results yet (<0.85 confidence) — try LLM
    if not results or max((r.confidence for r in results), default=0.0) < 0.85:
        try:
            llm_result = record("L9_llm_context", await l9_llm_context(db_path, ctx))
            if llm_result:
                results.append(llm_result)
        except Exception as exc:  # noqa: BLE001 - optional provider boundary
            states["L9_llm_context"] = {"status": "unavailable", "error_type": type(exc).__name__}

    if not results:
        return evidence({
            "category": "unknown",
            "confidence": 0.0,
            "reasoning": ["no_layer_matched"],
            "escalate": True,
            "layers_run": 0,
        })

    # Weighted vote
    weights: dict[str, float] = {}
    for r in results:
        if not r.category:
            continue
        w = r.weight * r.confidence
        cat = canonical_category(r.category)
        weights[cat] = weights.get(cat, 0.0) + w

    if not weights:
        return evidence({
            "category": "unknown",
            "confidence": 0.0,
            "reasoning": ["no_valid_category"],
            "escalate": True,
            "layers_run": len(results),
        })

    best = max(weights.items(), key=lambda x: x[1])
    total_weight = sum(r.weight for r in results if r.category)
    final_conf = min(best[1] / max(total_weight, 1.0), 1.0)

    # Consistency check: if top-2 categories are close -> escalate & reduce confidence
    sorted_w = sorted(weights.values(), reverse=True)
    if len(sorted_w) >= 2 and sorted_w[0] / max(sorted_w[1], 0.01) < 1.5:
        final_conf *= 0.7  # reduce confidence if conflict

    escalate = final_conf < 0.6

    return evidence({
        "category": best[0],
        "confidence": final_conf,
        "reasoning": [f"{r.layer}: {r.reasoning}" for r in results],
        "escalate": escalate,
        "layers_run": len(results),
    })
