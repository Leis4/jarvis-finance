"""Versioned economic categories, separate from lossy UI grouping."""

from jarvis.finance.categories import CATEGORY_DEFS
from jarvis.finance.classification.taxonomy import OUTGOING_CLASSES

TAXONOMY_VERSION = "economic-v1"
UNRESOLVED_CATEGORIES = frozenset({"pending_review", "unknown", "uncategorized", ""})
ALIASES = {"investment_buy": "investment_purchase", "investment_transfer": "investment_principal"}
INVESTMENT_PRINCIPAL = frozenset({"investment_purchase", "investment_principal", "investment_sale"})
EXPENSE_CATEGORIES = frozenset(
    key for top, group in OUTGOING_CLASSES.items() if top not in {'investment', 'cash', 'unknown'}
    for key in (top, *group['aliases'])
) | frozenset(c.key for c in CATEGORY_DEFS if c.necessity not in {'income', 'transfer', 'unknown'}) | {
    'games', 'peer_transfer',
}
CATEGORY_TITLES = {
    "food": "Питание", "food_basic": "Продукты", "cafes": "Кафе", "delivery": "Доставка",
    "transport": "Транспорт", "health": "Здоровье", "subscriptions": "Подписки",
    "shopping_marketplace": "Маркетплейсы", "bills": "Обязательные платежи",
    "alcohol_tobacco": "Алкоголь и табак", "games_digital": "Цифровые покупки",
    "salary": "Зарплата", "stipend": "Стипендия", "family_income": "Поддержка семьи",
    "refund": "Возврат", "peer_transfer": "Перевод третьему лицу",
    "peer_reimbursement": "Друзья вернули",
    "parent_transfer_in": "Перевод от родителей", "parent_transfer_out": "Перевод родителям",
    "sibling_transfer_in": "Перевод от родственника",
    "sibling_transfer_out": "Перевод родственнику",
    "friend_transfer_in": "Перевод от друга", "friend_transfer_out": "Перевод другу",
    "friend_reimbursement": "Друг вернул долг",
    "cash_withdrawal": "Снятие наличных", "cash_deposit": "Внесение наличных",
    "loan_principal": "Основная сумма займа", "savings_transfer": "Накопления",
}


def category_title(category: str) -> str:
    """Human label for answers; unknown codes stay visible instead of invented."""
    canonical = canonical_category(category)
    return CATEGORY_TITLES.get(canonical, canonical)
NON_ECONOMIC_CATEGORIES = frozenset({
    "internal_transfer", "transfer", "savings_transfer", "income_transfer",
    "cash_withdrawal", "cash_deposit", "loan_principal", "installment_principal",
}) | INVESTMENT_PRINCIPAL


def canonical_category(category: str) -> str:
    """Normalize only documented aliases; never infer transaction facts."""
    return ALIASES.get(category, category)
