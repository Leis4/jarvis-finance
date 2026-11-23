"""2-level taxonomy: top classes + learned subtypes."""
from typing import Any

# TOP-LEVEL CLASSES (visible in UI)
INCOMING_CLASSES = {
    "family_income": {
        "label": "👨‍👩‍👦 Поддержка семьи",
        "aliases": ["family_income", "parental_support", "family_support"],
        "subtypes": ["from_father", "from_mother"],
    },
    "peer_transfer_in": {
        "label": "💸 Перевод от друга",
        "aliases": ["peer_transfer_in", "peer_transfer"],
        "subtypes": [],
    },
    "stipend": {
        "label": "🎓 Стипендия",
        "aliases": ["stipend", "scholarship"],
        "subtypes": [],
    },
    "compensation": {
        "label": "💸 Компенсация",
        "aliases": ["compensation", "reimbursement"],
        "subtypes": ["from_friend", "from_relative"],
    },
    "gift": {
        "label": "🎁 Подарок",
        "aliases": ["gift"],
        "subtypes": [],
    },
    "salary": {
        "label": "💼 Зарплата",
        "aliases": ["salary", "income"],
        "subtypes": [],
    },
    "unknown": {
        "label": "❓ Другое",
        "aliases": ["unknown", "other"],
        "subtypes": [],
    },
}


OUTGOING_CLASSES = {
    "food": {
        "label": "🍔 Еда",
        "aliases": ["food_basic", "cafes", "delivery", "food_delivery", "restaurant"],
        "subtypes": ["groceries", "cafe", "delivery", "restaurant"],
    },
    "transport": {
        "label": "🚕 Транспорт",
        "aliases": ["transport", "micromobility", "taxi"],
        "subtypes": ["taxi", "metro", "scooter", "fuel"],
    },
    "entertainment": {
        "label": "🎬 Развлечение",
        "aliases": ["entertainment", "games_digital", "hobby", "vape",
                    "alcohol_tobacco", "smoking"],
        "subtypes": ["cinema", "games", "vape", "alcohol", "hobby"],
    },
    "health": {
        "label": "💊 Здоровье",
        "aliases": ["health", "medication", "pharmacy", "sport", "fitness"],
        "subtypes": ["medication", "doctor", "sport", "fitness"],
    },
    "housing": {
        "label": "🏠 Жильё",
        "aliases": ["housing_utilities", "rent", "utility", "education_housing"],
        "subtypes": ["rent", "utilities", "dorm"],
    },
    "bills": {
        "label": "📱 Связь/подписки",
        "aliases": ["bills", "subscriptions", "internet", "mobile"],
        "subtypes": ["internet", "mobile", "streaming", "software"],
    },
    "shopping": {
        "label": "🛒 Покупки",
        "aliases": ["shopping_marketplace", "shopping", "clothes"],
        "subtypes": ["marketplace", "clothes", "electronics"],
    },
    "transfer_out": {
        "label": "💸 Перевод другу",
        "aliases": ["peer_transfer_out", "compensation_out",
                    "family_transfer_out", "family_expense"],
        "subtypes": [],
    },
    "investment": {
        "label": "📈 Инвестиции",
        "aliases": ["investment_buy", "investment_transfer", "investment_income",
                    "investment_purchase", "investment_principal", "investment_sale"],
        "subtypes": ["buy", "transfer", "income"],
    },
    "cash": {
        "label": "💵 Наличные",
        "aliases": ["cash_withdrawal", "cash_deposit"],
        "subtypes": ["withdrawal", "deposit"],
    },
    "unknown": {
        "label": "❓ Другое",
        "aliases": ["unknown", "other"],
        "subtypes": [],
    },
}


def get_ui_classes(direction: str) -> list[dict[str, Any]]:
    """Return UI buttons for given direction."""
    classes = INCOMING_CLASSES if direction == "incoming" else OUTGOING_CLASSES
    return [
        {"label": v["label"], "value": key}
        for key, v in classes.items()
    ]


def normalize_category(raw: str) -> str:
    """Map raw/legacy category to top-level class."""
    raw_low = (raw or "").lower()
    for top, meta in {**INCOMING_CLASSES, **OUTGOING_CLASSES}.items():
        if raw_low == top or raw_low in meta["aliases"]:
            return top
    return "unknown"
