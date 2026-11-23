from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CategoryDef:
    key: str
    label: str
    bucket: str
    necessity: str
    cut: Decimal
    floor_monthly: Decimal
    description: str


# User-facing taxonomy. Keep keys stable: they are stored in DB and review overrides.
CATEGORY_DEFS: list[CategoryDef] = [
    CategoryDef("food_basic", "Продукты / нормальная еда", "Еда", "essential", Decimal("0.08"), Decimal(2800), "Пятёрочка, Магнит, Дикси, Лента, базовые продукты."),
    CategoryDef("snacks_drinks", "Снеки / сладкое / напитки", "Еда", "flex", Decimal("0.45"), Decimal(0), "Чипсы, сладкое, газировка — режется первым."),
    CategoryDef("alcohol_tobacco", "Алкоголь / табак / вредные мелочи", "Еда", "wants", Decimal("0.85"), Decimal(0), "Красное&Белое, табак, smoke и похожие траты — максимально урезать."),
    CategoryDef("food_delivery_grocery", "Доставка продуктов", "Еда", "flex", Decimal("0.25"), Decimal(0), "Яндекс Лавка и похожие доставки продуктов."),
    CategoryDef("cafes", "Кафе / фастфуд / кофе", "Еда", "flex", Decimal("0.55"), Decimal(0), "Кафе, шаурма, Вкусно и точка, Додо, Burger King, кофе с собой."),
    CategoryDef("delivery", "Доставка еды", "Еда", "flex", Decimal("0.65"), Decimal(0), "Яндекс Еда и готовая еда домой."),
    CategoryDef("housing_utilities", "Жильё / коммуналка / обязательный платёж", "Обязательное", "essential", Decimal("0.00"), Decimal(0), "Крупные обязательные платежи: жильё, общежитие, коммуналка, связь/интернет."),
    CategoryDef("transport", "Транспорт", "Обязательное", "essential", Decimal("0.05"), Decimal(1200), "Метро, МЦД, автобус, Aeroexpress."),
    CategoryDef("taxi", "Такси", "Гибкие траты", "flex", Decimal("0.55"), Decimal(0), "Яндекс Go и такси."),
    CategoryDef("micromobility", "Самокаты / аренда", "Гибкие траты", "flex", Decimal("0.65"), Decimal(0), "Самокаты, Urent, scooters."),
    CategoryDef("laundry", "Стирка белья", "Быт", "semi", Decimal("0.10"), Decimal(400), "Прачечная/стирка, MTA и похожие регулярные бытовые траты."),
    CategoryDef("home", "Быт / дом / расходники", "Быт", "semi", Decimal("0.20"), Decimal(300), "Домашние мелочи, бытовые расходники."),
    CategoryDef("health_lenses", "Линзы / здоровье / аптека", "Здоровье", "semi", Decimal("0.10"), Decimal(300), "Раствор для линз, аптека, здоровье."),
    CategoryDef("personal_care", "Гигиена / уход / бытовая химия", "Быт", "semi", Decimal("0.15"), Decimal(250), "Шампунь, порошок, гель, уход, расходники."),
    CategoryDef("sport", "Спорт / зал", "Здоровье", "semi", Decimal("0.15"), Decimal(1500), "Абонемент/зал/спорт."),
    CategoryDef("subscriptions", "Подписки / связь / VPN", "Цифровое", "semi", Decimal("0.25"), Decimal(300), "VK, Яндекс Плюс, VPN, Timeweb, интернет."),
    CategoryDef("installment_yandex_split", "Яндекс Сплит / рассрочки", "Долги", "debt", Decimal("0.00"), Decimal(0), "Обязательные платежи рассрочек; новые рассрочки запрещать."),
    CategoryDef("games_digital", "Игры / цифровые товары", "Хотелки", "wants", Decimal("0.70"), Decimal(0), "FunPay, GGS, PlayerOK, Plati, Robokassa software."),
    CategoryDef("shopping_marketplace", "Маркетплейсы / покупки", "Покупки", "flex", Decimal("0.45"), Decimal(0), "Ozon, Wildberries, Яндекс Маркет, если не уточнено."),
    CategoryDef("clothes", "Одежда", "Покупки", "flex", Decimal("0.35"), Decimal(0), "Одежда и обувь."),
    CategoryDef("electronics", "Электроника", "Покупки", "wants", Decimal("0.60"), Decimal(0), "Техника, комплектующие, аксессуары."),
    CategoryDef("computer_purchase", "ПК / комплектующие", "Крупные покупки", "goal", Decimal("0.00"), Decimal(0), "Покупка ПК/комплектующих — только через цель, не из еды."),
    CategoryDef("large_purchase", "Крупная покупка", "Крупные покупки", "goal", Decimal("0.00"), Decimal(0), "Большой перевод/покупка — требует подтверждения."),
    CategoryDef("peer_transfer", "Друзья / скинулись", "Переводы", "transfer", Decimal("0.00"), Decimal(0), "Твой перевод друзьям, делёжка."),
    CategoryDef("peer_reimbursement", "Друзья вернули", "Переводы", "transfer", Decimal("0.00"), Decimal(0), "Тебе вернули/скинулись."),
    CategoryDef("parent_transfer_in", "Перевод от родителей", "Семья", "income", Decimal("0.00"), Decimal(0), "Родители прислали деньги."),
    CategoryDef("parent_transfer_out", "Перевод родителям", "Семья", "transfer", Decimal("0.00"), Decimal(0), "Ты перевёл деньги родителям."),
    CategoryDef("sibling_transfer_in", "Перевод от родственника", "Семья", "income", Decimal("0.00"), Decimal(0), "Брат/сестра/родня прислала деньги."),
    CategoryDef("sibling_transfer_out", "Перевод родственнику", "Семья", "transfer", Decimal("0.00"), Decimal(0), "Ты перевёл деньги родственнику."),
    CategoryDef("friend_transfer_in", "Перевод от друга", "Друзья", "income", Decimal("0.00"), Decimal(0), "Друг прислал деньги (не возврат покупки)."),
    CategoryDef("friend_transfer_out", "Перевод другу", "Друзья", "transfer", Decimal("0.00"), Decimal(0), "Ты перевёл деньги другу."),
    CategoryDef("friend_reimbursement", "Друг вернул долг", "Друзья", "income", Decimal("0.00"), Decimal(0), "Возврат долга или совместной покупки."),
    CategoryDef("internal_transfer", "Между моими счетами", "Переводы", "transfer", Decimal("0.00"), Decimal(0), "Сбер ↔ Альфа/Т-Банк/РСХБ/вклад/себе."),
    CategoryDef("savings_transfer", "Перевод в сберегательный счёт", "Переводы", "transfer", Decimal("0.00"), Decimal(0), "Деньги ушли в копилку/сберегательный счёт, это не расход."),
    CategoryDef("invest_kopilka", "ИнвестКопилка", "Инвестиции", "transfer", Decimal("0.00"), Decimal(0), "Перевод в ИнвестКопилку/брокерский счёт, учитывать отдельно от расходов."),
    CategoryDef("family_support", "Родители / деньги на жизнь", "Доходы", "income", Decimal("0.00"), Decimal(0), "Ольга/Николай, деньги на жизнь."),
    CategoryDef("scholarship", "Стипендия", "Доходы", "income", Decimal("0.00"), Decimal(0), "Стипендия/вуз/РСХБ."),
    CategoryDef("refund", "Возврат", "Доходы", "income", Decimal("0.00"), Decimal(0), "Возврат покупки/отмена."),
    CategoryDef("cash", "Наличные", "Переводы", "transfer", Decimal("0.00"), Decimal(0), "Снятие/внесение наличных."),
    CategoryDef("education_print", "Учёба / печать / документы", "Учёба", "semi", Decimal("0.15"), Decimal(150), "Копицентр, печать, документы, учебные мелочи."),
    CategoryDef("other_wants", "Прочие хотелки", "Хотелки", "wants", Decimal("0.60"), Decimal(0), "Непонятные гибкие траты, пока не уточнены."),
    CategoryDef("uncategorized", "Не распознано", "Проверка", "unknown", Decimal("0.50"), Decimal(0), "Надо выбрать категорию вручную."),
]

CATEGORY_BY_KEY = {c.key: c for c in CATEGORY_DEFS}

ALIASES = {
    "food": "food_basic",
    "wants_supermarket": "snacks_drinks",
    "wants": "snacks_drinks",
    "smoke": "alcohol_tobacco",
    "shopping": "shopping_marketplace",
    "real_purchase": "shopping_marketplace",
    "transfer": "peer_transfer",
    "delivery": "delivery",
}

EXCLUDED_FROM_SPENDING = {"internal_transfer", "savings_transfer", "invest_kopilka", "peer_reimbursement", "family_support", "scholarship", "refund", "cash"}


def normalize_category(category: str | None) -> str:
    key = (category or "uncategorized").strip().lower()
    return ALIASES.get(key, key if key in CATEGORY_BY_KEY else "uncategorized")


def label(category: str | None) -> str:
    key = normalize_category(category)
    return CATEGORY_BY_KEY.get(key, CATEGORY_BY_KEY["uncategorized"]).label


def bucket(category: str | None) -> str:
    key = normalize_category(category)
    return CATEGORY_BY_KEY.get(key, CATEGORY_BY_KEY["uncategorized"]).bucket


def review_options() -> list[tuple[str, str]]:
    return [(c.key, c.label) for c in CATEGORY_DEFS]


def category_select_options(current: str | None = None) -> str:
    cur = normalize_category(current)
    grouped: dict[str, list[CategoryDef]] = {}
    for item in CATEGORY_DEFS:
        grouped.setdefault(item.bucket, []).append(item)
    html = []
    for group, items in grouped.items():
        html.append(f"<optgroup label='{group}'>")
        for item in items:
            selected = " selected" if item.key == cur else ""
            html.append(f"<option value='{item.key}'{selected}>{item.label}</option>")
        html.append("</optgroup>")
    return "".join(html)
