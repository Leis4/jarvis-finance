"""Explainable Russian merchant normalization and local MCC hints."""
from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz

from jarvis.finance.classification.learning import normalized

# Curated aliases only; a high fuzzy score never merges two different brands.
ALIASES = {
    'yandex food': 'yandex eda',
    'yandex.eda': 'yandex eda',
    'yandeks eda': 'yandex eda',
    'yandeks food': 'yandex eda',
    'yandex go': 'yandex taxi',
    'yandex.taxi': 'yandex taxi',
    'yandeks go': 'yandex taxi',
    'yandeks taxi': 'yandex taxi',
    'pyaterochka': 'pyaterochka',
    '5ka': 'pyaterochka',
}


def normalize_merchant(value: str) -> str:
    text = normalized(value)
    return ALIASES.get(text, text)


def match_merchant(query: str, candidates: list[str], *, threshold: float = 0.90) -> dict[str, Any]:
    """Return a ranked, explainable match; callers decide whether to use it."""
    left = normalize_merchant(query)
    if not left:
        return {'status': 'empty', 'match': None, 'score': 0.0, 'reason': 'empty'}
    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        right = normalize_merchant(candidate)
        score = fuzz.ratio(left, right) / 100
        if score >= threshold:
            scored.append({'merchant': candidate, 'normalized': right, 'score': round(score, 3),
                'reason': 'exact_alias' if left == right else 'fuzzy_name'})
    scored.sort(key=lambda row: float(row['score']), reverse=True)
    if not scored:
        return {'status': 'no_match', 'match': None, 'score': 0.0, 'reason': 'below_threshold'}
    return {'status': 'match', **scored[0], 'candidates': scored[:5],
        'notice': 'Совпадение названия не доказывает одинаковое назначение покупки.'}


# Compact subset for local hints. Source: ISO 18245 MCC descriptions; no network.
# Owner-confirmed categories always win over this hint.
MCC_MAP = {
    '5411': ('food_basic', 'Супермаркеты, продуктовые магазины'),
    '5812': ('cafes', 'Рестораны, кафе'),
    '5813': ('cafes', 'Бары, ночные клубы'),
    '4121': ('transport', 'Такси, лимузины'),
    '4111': ('transport', 'Транспорт, метро'),
    '5912': ('health', 'Аптеки'),
    '5651': ('shopping_marketplace', 'Магазины одежды'),
    '5732': ('games_digital', 'Электроника, цифровые товары'),
    '4899': ('subscriptions', 'Кабельное/цифровое телевидение'),
    '5968': ('subscriptions', 'Периодические подписки'),
    '6011': ('cash_withdrawal', 'Снятие наличных'),
    '6012': ('loan_principal', 'Финансовые учреждения'),
    '4829': ('peer_transfer', 'Денежные переводы'),
}


def mcc_hint(mcc: str | int | None) -> dict[str, Any] | None:
    """Return a hint only; never write a category or override an owner label."""
    code = ''.join(ch for ch in str(mcc or '') if ch.isdigit()).zfill(4)[-4:]
    item = MCC_MAP.get(code)
    if not item:
        return None
    return {'mcc': code, 'category': item[0], 'title': item[1], 'hint_only': True}
