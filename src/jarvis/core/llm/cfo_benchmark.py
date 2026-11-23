"""Fixed synthetic classification benchmark; no owner observations or training data."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from jarvis.core.llm.intelligence import MODELS, Intelligence
from jarvis.core.llm.pricing import aitunnel_price_evidence
from jarvis.finance.classification.intelligence import (
    Features,
    prompt_for,
    validate_decision,
)

CASES: tuple[tuple[str, str, list[str], list[str], str], ...] = (
    ('food', 'debit', ['food'], [], 'food_basic'),
    ('transport', 'debit', ['transport'], [], 'transport'),
    ('refund', 'credit', ['refund'], [], 'refund'),
    ('cash_out', 'debit', ['cash_withdrawal'], [], 'cash_withdrawal'),
    ('cash_in', 'credit', ['cash_deposit'], [], 'cash_deposit'),
    ('loan', 'debit', ['loan'], [], 'loan_principal'),
    ('own_transfer', 'debit', ['own_transfer_proven'], [], 'internal_transfer'),
    ('family', 'credit', ['family'], [], 'family_income'),
    ('conflict', 'debit', ['marketplace'], ['shopping_marketplace', 'health'], 'unknown'),
    ('insufficient', 'credit', [], [], 'unknown'),
)


def benchmark_prompt() -> str:
    base = prompt_for(Features(direction='debit'))
    rows = [{'id': ident, 'direction': direction, 'signals': signals, 'confirmed_categories': confirmed}
        for ident, direction, signals, confirmed, _ in CASES]
    return base + '\nВместо одного объекта классифицируй каждый тестовый пример. Ответ только ' + \
        '{"decisions":[{"id":str,"category":str,"reason":str,"review":bool}]}. Тесты: ' + json.dumps(rows, ensure_ascii=False)


def checked_benchmark(value: dict[str, Any]) -> dict[str, Any]:
    decisions = value['decisions']
    if not isinstance(decisions, list) or len(decisions) != len(CASES):
        raise ValueError('Complete fixed benchmark required')
    by_id = {row['id']: row for row in decisions}
    if set(by_id) != {row[0] for row in CASES}:
        raise ValueError('Exact case identities required')
    checked = []
    for ident, direction, signals, confirmed, expected in CASES:
        row = by_id[ident]
        item = validate_decision({k: v for k, v in row.items() if k != 'id'},
            Features(direction=direction, signals=signals, confirmed_categories=confirmed))  # type: ignore[arg-type]
        checked.append({'id': ident, 'expected': expected, **item})
    correct = sum(row['category'] == row['expected'] for row in checked)
    labels = {row['expected'] for row in checked} | {row['category'] for row in checked}
    scores = []
    for label in labels:
        tp = sum(r['category'] == label and r['expected'] == label for r in checked)
        fp = sum(r['category'] == label and r['expected'] != label for r in checked)
        fn = sum(r['category'] != label and r['expected'] == label for r in checked)
        scores.append(2*tp / max(1, 2*tp+fp+fn))
    return {'accuracy': correct/len(CASES), 'macro_f1': sum(scores)/len(scores), 'cases': checked,
        'scope': '10 synthetic screened cases, one batch per model; not personal Sber accuracy or calibrated confidence'}


async def run_benchmark(runtime: Intelligence, *, maximum: Decimal = Decimal(2)) -> dict[str, Any]:
    """At most three paid calls, no fallback/retry, total pessimistic reserve <= maximum."""
    prompt = benchmark_prompt()
    now = datetime.now(UTC)
    reserves = {model: aitunnel_price_evidence(runtime.pricing, model, now=now,
        input_cap=len(prompt.encode())+512, output_cap=1024).reserved_rub.quantize(Decimal('.01'), rounding=ROUND_CEILING)
        for model in MODELS}
    if sum(reserves.values(), Decimal(0)) > maximum:
        raise ValueError('Benchmark maximum too small for all worst-case reserves')
    before = Decimal(runtime.journal.summary(now=now)['settled_month'])
    results = []
    from jarvis.core.llm.orchestrator import complete_intelligence
    for model in MODELS:
        used = Decimal(runtime.journal.summary(now=datetime.now(UTC))['settled_month']) - before
        if used + reserves[model] > maximum:
            break
        result = await complete_intelligence('benchmark', prompt, checked_benchmark, runtime=runtime, model=model,
            max_output=1024, fallback=False, ceiling=maximum-used)
        results.append({'candidate': model, **result})
        if result['status'] in {'unknown', 'not_repeated', 'budget_refused'}:
            break
    cost = Decimal(runtime.journal.summary(now=datetime.now(UTC))['settled_month']) - before
    return {'version': 1, 'observed_at': datetime.now(UTC).isoformat(), 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'maximum_rub': str(maximum), 'worst_case_reserves_rub': {k: str(v) for k, v in reserves.items()},
        'actual_cost_rub': str(cost), 'results': results, 'production_accuracy_verified': False}


async def main(output: Path) -> None:
    """Explicit owner CLI only; never called by tests, scheduler or application startup."""
    if output.exists():
        raise ValueError('Existing benchmark output; no paid repetition')
    result = await run_benchmark(Intelligence.configured())
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    import argparse
    import asyncio
    parser = argparse.ArgumentParser(description='Owner-approved synthetic AI benchmark, hard maximum 2 RUB')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.output))
