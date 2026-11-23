"""Markov chain transition model for spending category prediction."""

from __future__ import annotations

from typing import Any

NON_SPEND_CATEGORIES = {
    "transfer",
    "internal_transfer",
    "savings_transfer",
    "stipend",
    "parental",
    "compensation",
    "gift",
    "unknown_incoming",
}


def build_transition_matrix(
    transactions: list[dict[str, Any]],
    alpha: float = 0.1,
) -> dict[str, dict[str, float]]:
    """Build a Markov transition matrix with Laplace smoothing.

    P(category_next | category_current) = (count + alpha) / (total + alpha * num_categories)
    """
    # Sort transactions chronologically
    sorted_txs = sorted(
        transactions,
        key=lambda x: (str(x.get("tx_date", "")), str(x.get("id", ""))),
    )

    # Filter for expense transactions with valid categories
    sequence: list[str] = []
    for tx in sorted_txs:
        amt = float(tx.get("amount", 0))
        cat = str(tx.get("category", "")).strip().lower()
        if not cat:
            continue
        # Filter out non-spending or positive transfers
        if amt < 0 and cat not in NON_SPEND_CATEGORIES:
            sequence.append(cat)

    if len(sequence) < 2:
        return {}

    unique_categories = sorted(set(sequence))
    num_categories = len(unique_categories)
    if num_categories == 0:
        return {}

    # Count transitions
    counts: dict[str, dict[str, int]] = {c: {next_c: 0 for next_c in unique_categories} for c in unique_categories}
    for i in range(len(sequence) - 1):
        c_curr = sequence[i]
        c_next = sequence[i + 1]
        if c_curr == c_next:  # skip self-transitions
            continue
        counts[c_curr][c_next] += 1

    # Apply Laplace smoothing
    matrix: dict[str, dict[str, float]] = {}
    for c_curr, next_dict in counts.items():
        total_transitions = sum(next_dict.values())
        denominator = total_transitions + alpha * num_categories
        matrix[c_curr] = {}
        for c_next, count in next_dict.items():
            prob = (count + alpha) / denominator
            matrix[c_curr][c_next] = round(prob, 4)

    return matrix


def predict_next_category(
    current_category: str,
    transition_matrix: dict[str, dict[str, float]],
    top_n: int = 3,
) -> list[tuple[str, float]]:
    """Predict top N most probable next categories given current category."""
    cat = current_category.strip().lower()
    if cat not in transition_matrix:
        return []

    row = transition_matrix[cat]
    # Sort by probability descending
    sorted_row = sorted(row.items(), key=lambda x: -x[1])
    return [(k, v) for k, v in sorted_row[:top_n]]


def extract_markov_insights(
    transition_matrix: dict[str, dict[str, float]],
    min_probability: float = 0.15,
    min_count: int = 2,
    top_n: int = 3,
    min_confidence: float | None = None,
) -> list[str]:
    """Generate textual Markov insights for transitions above min_probability."""
    if min_confidence is not None:
        min_probability = min_confidence

    insights: list[tuple[float, str]] = []
    for curr_cat, next_probs in transition_matrix.items():
        # Find top transitions
        top = [
            (target, prob)
            for target, prob in sorted(next_probs.items(), key=lambda x: -x[1])
            if prob >= min_probability
        ]
        if top:
            top_str = ", ".join(f"'{target}' ({round(prob * 100)}%)" for target, prob in top[:2])
            insights.append((top[0][1], f"После '{curr_cat}' обычно {top_str}"))

    # Sort by probability descending so strongest patterns come first
    insights.sort(key=lambda x: -x[0])
    return [msg for _, msg in insights[:top_n]]


__all__ = [
    "NON_SPEND_CATEGORIES",
    "build_transition_matrix",
    "extract_markov_insights",
    "predict_next_category",
]
