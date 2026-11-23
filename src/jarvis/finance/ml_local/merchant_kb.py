"""Merchant Knowledge Base — Russian merchants mapped to categories."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MerchantEntry:
    patterns: list[str]  # lowercase substrings to match
    category: str
    source: str = "kb"  # "kb", "sber", "ozon", etc.


MERCHANT_KB: list[MerchantEntry] = [
    # === food_basic (супермаркеты) ===
    MerchantEntry(["магнит", "magnit"], "food_basic"),
    MerchantEntry(["пятёрочка", "пятерочка", "5ka", "pyaterochka", "35n9 пятерочка"], "food_basic"),
    MerchantEntry(["вкусвилл", "vkusvill"], "food_basic"),
    MerchantEntry(["перекрёсток", "перекресток", "perekrestok"], "food_basic"),
    MerchantEntry(["лента", "lenta"], "food_basic"),
    MerchantEntry(["ашан", "auchan"], "food_basic"),
    MerchantEntry(["дикси", "dixy"], "food_basic"),
    MerchantEntry(["метро", "metro cash"], "food_basic"),
    MerchantEntry(["окей", "okey", "o'key"], "food_basic"),
    MerchantEntry(["спар", "spar"], "food_basic"),
    MerchantEntry(["азбука вкуса", "azbuka vkusa"], "food_basic"),
    MerchantEntry(["градусы", "покупалко", "чижик", "верный"], "food_basic"),

    # === alcohol_tobacco (алкоголь и табак) ===
    MerchantEntry(["красное&белое", "красное белое", "красноебелое", "к&б", "бристоль"], "alcohol_tobacco"),

    # === delivery / food_delivery (доставка еды) ===
    MerchantEntry(["самокат", "samokat"], "delivery"),
    MerchantEntry(["янндекс еда", "яндекс.еда", "yandex eda", "yandex.eda"], "food_delivery"),
    MerchantEntry(["янндекс лавка", "яндекс.лавка", "yandex lavka", "yandex.lavka"], "food_delivery"),
    MerchantEntry(["деливери", "delivery club", "купер", "kuper"], "food_delivery"),
    MerchantEntry(["додо пицца", "dodo pizza", "dodo"], "delivery"),
    MerchantEntry(["вкусвилл доставка"], "delivery"),
    MerchantEntry(["магаз ритейл", "уконтейнера"], "delivery"),

    # === shopping_marketplace (маркетплейсы и онлайн-шопинг) ===
    MerchantEntry(["озон", "ozon", "ооо \"интернет решения\"", "интернет решения"], "shopping_marketplace"),
    MerchantEntry(["вайлдберриз", "wildberries", "wb"], "shopping_marketplace"),
    MerchantEntry(["яндекс маркет", "яндекс.маркет", "yandex market", "yandex.market"], "shopping_marketplace"),
    MerchantEntry(["сбермегамаркет", "мегамаркет", "sbermegamarket"], "shopping_marketplace"),
    MerchantEntry(["авито", "avito"], "shopping_marketplace"),
    MerchantEntry(["казаньэкспресс", "mmk"], "shopping_marketplace"),

    # === games_digital (игры и цифровой контент) ===
    MerchantEntry(["funpay", "фанпей"], "games_digital"),
    MerchantEntry(["steam", "steampowered"], "games_digital"),
    MerchantEntry(["playstation", "psn"], "games_digital"),
    MerchantEntry(["xbox", "microsoft store"], "games_digital"),
    MerchantEntry(["epic games", "epicgames"], "games_digital"),
    MerchantEntry(["gaijin", "wargaming", "lesta"], "games_digital"),
    MerchantEntry(["vk play", "vkplay"], "games_digital"),

    # === subscriptions (подписки и AI сервисы) ===
    MerchantEntry(["aitunnel", "aitunnel.ru"], "subscriptions"),
    MerchantEntry(["яндекс плюс", "яндекс.плюс", "yandex plus", "yandex.plus"], "subscriptions"),
    MerchantEntry(["vk combo", "vk rus", "vk музыка", "vk music"], "subscriptions"),
    MerchantEntry(["кинопоиск", "kinopoisk"], "subscriptions"),
    MerchantEntry(["иви", "ivi"], "subscriptions"),
    MerchantEntry(["окко", "okko"], "subscriptions"),
    MerchantEntry(["кнопка", "kion"], "subscriptions"),
    MerchantEntry(["chatgpt", "openai", "claude", "anthropic"], "subscriptions"),
    MerchantEntry(["boosty", "бусти"], "subscriptions"),
    MerchantEntry(["букмейт", "bookmate"], "subscriptions"),

    # === micromobility (кикшеринг и самокаты) ===
    MerchantEntry(["yandex*scooters", "яндекс самокаты", "yandex scooters"], "micromobility"),
    MerchantEntry(["вуш", "whoosh"], "micromobility"),
    MerchantEntry(["юрент", "urent", "ym*urent"], "micromobility"),

    # === transport / taxi (общественный транспорт и такси) ===
    MerchantEntry(["яндекс такси", "yandex taxi", "yandex.go", "яндекс go"], "taxi"),
    MerchantEntry(["ситимобил", "citimobil"], "taxi"),
    MerchantEntry(["убер", "uber"], "taxi"),
    MerchantEntry(["метрополитен", "московский транспорт", "mta moscow", "транспортная карта", "тройка", "стрелка", "пасстранс"], "transport"),
    MerchantEntry(["аэроэкспресс", "aeroexpress"], "transport"),
    MerchantEntry(["ржд", "rzhd"], "transport"),

    # === cafes (кафе и фастфуд) ===
    MerchantEntry(["шаурма", "татмак", "старбакс", "starbucks"], "cafes"),
    MerchantEntry(["burger king", "бургер кинг", "жар свежар", "цех", "qsr", "мир вендинга", "вендинг"], "cafes"),

    # === food_basic (супермаркеты) ===
    MerchantEntry(["перекрёсток", "перекресток"], "food_basic"),

    # === alcohol_tobacco (алкоголь и табак) ===
    MerchantEntry(["tabaktabakoff", "табак"], "alcohol_tobacco"),

    # === subscriptions (подписки и облачные сервисы) ===
    MerchantEntry(["yandex*oblako", "яндекс облако", "yandex.oblako", "yandex oblako"], "subscriptions"),

    # === bills (связь, коммуналка, сервисы) ===
    MerchantEntry(["yandex*ysplit", "yandex.ysplit"], "bills"),
    MerchantEntry(["kopitsentr", "копицентр", "ip semin", "ip razina"], "bills"),

    # === health (аптеки и медицина) ===
    MerchantEntry(["ooo ldc farm-t", "ldc farm"], "health"),

    # === transport / parking (транспорт и парковки) ===
    MerchantEntry(["park ch-e ozero", "парковка", "prigorodnye kassy", "kassa khimki"], "transport"),

    # === installment_yandex_split (Сплит) ===
    MerchantEntry(["yandex*split", "яндекс сплит", "яндекс.сплит", "yandex split"], "installment_yandex_split"),
]


def get_kb_by_category() -> dict[str, list[str]]:
    """Return dictionary mapping category -> list of merchant pattern strings."""
    res: dict[str, list[str]] = {}
    for entry in MERCHANT_KB:
        if entry.category not in res:
            res[entry.category] = []
        for pat in entry.patterns:
            if pat not in res[entry.category]:
                res[entry.category].append(pat)
    return res


def extract_merchant_from_text(text: str) -> str:
    """Extract primary merchant keyword or first word from transaction description."""
    text_lower = text.lower()
    for entry in MERCHANT_KB:
        for pat in entry.patterns:
            if pat in text_lower:
                return pat
    # Fallback to first word > 2 chars
    words = text.split()
    if words and len(words[0]) > 2:
        return words[0]
    return text[:15]
