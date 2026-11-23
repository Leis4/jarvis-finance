"""Closed offline Russian calculator routing; no inferred financial inputs."""
from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from jarvis.services.chat_scenarios import ChatScenario

NUMBER = r"-?\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?"
MONEY = rf"({NUMBER})\s*(тысяч\w*|тыс\.?|миллион\w*|млн\.?)?"


def _money(match: re.Match[str]) -> str:
    value = Decimal(match[1].replace(" ", "").replace("\u00a0", "").replace(",", "."))
    suffix = match[2] or ""
    return str(value * (1000000 if suffix.startswith(("миллион", "млн")) else 1000 if suffix else 1))


def russian_scenario(message: str, turns: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """Return explicit parameters or a clarification, within a fifteen-minute turn context."""
    query = " ".join(message.casefold().split())
    if re.match(r"(?:что такое|объясни(?:,)?(?: что такое)?)\b", query):
        return None  # Definitions use evidence retrieval, not scenario arithmetic.
    modes = [mode for pattern, mode in (
        (r"кредит", "hypothetical_loan"),
        (r"положить|вклад|капитализац", "hypothetical_compound"),
        (r"инфляци", "hypothetical_inflation"),
        (r"накопить|откладывать", "hypothetical_goal"),
    ) if re.search(pattern, query)]
    followup = bool(re.search(r"^(а если|пересчитай|ставка|срок|валюта|сумма|рубли|рублей|доллары|евро)\b", query))
    answered = [turn["answer"] for turn in turns if turn.get("answer")]
    prior = answered[-1] if answered else {}
    previous = prior.get("nl_parameters")
    if not modes and not followup:
        return None
    params: dict[str, Any] = {}
    if (followup or (modes == ["hypothetical_goal"] and "откладывать" in query)) and isinstance(previous, dict):
        stamp = datetime.fromisoformat(prior["nl_observed_at"])
        if stamp.tzinfo is not None and timedelta(0) <= now - stamp <= timedelta(minutes=15):
            params = dict(previous)
    if len(modes) > 1:
        return {"parameters": {}, "clarification": "Выберите один расчёт: кредит, вклад, накопление или инфляция."}
    if modes:
        if modes[0] == "hypothetical_inflation" and params.get("mode") != "hypothetical_inflation":
            params.pop("annual_rate_percent", None)  # Interest is not inflation.
            params.pop("target", None)
            params.pop("starting", None)
            params.pop("monthly_contribution", None)
            params.pop("deadline", None)
        elif not followup and "откладывать" not in query:
            params = {}
        params["mode"] = modes[0]
    if not params.get("mode"):
        return {"parameters": {}, "clarification": "Укажите расчёт явно: контекст отсутствует или истёк."}
    if re.search(r"-\s*\d", query):
        return {"parameters": {}, "clarification": "Отрицательные суммы, ставки и сроки не поддерживаются этим гипотетическим расчётом."}
    if re.search(r"\b(дней|недел\w*|ежеднев\w*|простые проценты|пск|эффективн\w*)\b", query):
        return {"parameters": params, "clarification": "Этот калькулятор поддерживает месяцы и номинальную годовую ставку. Уточните условия; ПСК и дневное начисление не рассчитываются."}
    currencies = {code for pattern, code in ((r"\bруб\w*\b|\brub\b|₽", "RUB"), (r"\bдоллар\w*\b|\busd\b|\$", "USD"), (r"\bевро\b|\beur\b|€", "EUR")) if re.search(pattern, query)}
    rates = re.findall(rf"({NUMBER})\s*%", query)
    terms = re.findall(rf"(?<![\d.,])({NUMBER})\s*(месяц\w*|лет|год\w*)", query)
    if len(currencies) > 1 or len(rates) > 1 or len(terms) > 1:
        return {"parameters": params, "clarification": "Есть несколько валют, ставок или сроков. Выберите по одному значению."}
    if currencies:
        params["currency"] = currencies.pop()
    if rates:
        if params["mode"] == "hypothetical_goal":
            return {"parameters": params, "clarification": "Накопление здесь рассчитывается без процентов. Для капитализации выберите вклад и задайте сумму, ставку и срок."}
        params["annual_rate_percent"] = rates[0].replace(",", ".").replace(" ", "")
    if terms:
        months = Decimal(terms[0][0].replace(",", ".").replace(" ", "")) * (1 if terms[0][1].startswith("месяц") else 12)
        if months != months.to_integral_value():
            return {"parameters": params, "clarification": "Укажите срок, соответствующий целому числу месяцев."}
        params["months"] = int(months)
    elif re.search(r"полтора года", query):
        params["months"] = 18
    elif re.search(r"(?:на|за) (?:один )?год\b", query):
        params["months"] = 12
    amounts = list(re.finditer(rf"(?:на|положить|сумма|накопить)\s+{MONEY}(?!\d)", query))
    amounts = [m for m in amounts if not re.match(r"\s*(?:месяц|год|лет|%)", query[m.end():])
               and not query[:m.start()].endswith("начальная ")]
    if len(amounts) > 1:
        return {"parameters": params, "clarification": "Укажите одну сумму для выбранного расчёта."}
    if amounts:
        params["target" if params["mode"] == "hypothetical_goal" else "principal"] = _money(amounts[0])
    if re.search(r"\b(?:gbp|cny|юан\w*|фунт\w*)\b", query):
        return {"parameters": params, "clarification": "Поддерживаются RUB, USD и EUR; обмен валют не выполняется."}
    if params["mode"] == "hypothetical_goal":
        for pattern, field in ((rf"(?:уже накоплено|начальная сумма)\s+{MONEY}", "starting"), (rf"(?:взнос|ежемесячно)\s+{MONEY}", "monthly_contribution")):
            found = list(re.finditer(pattern, query))
            if len(found) > 1:
                return {"parameters": params, "clarification": "Укажите однозначные начальную сумму и месячный взнос."}
            if found:
                params[field] = _money(found[0])
        required = ("target", "starting", "currency", "months")
    else:
        required = ("principal", "annual_rate_percent", "currency", "months")
    names = {"target": "цель", "starting": "начальная сумма (ноль тоже нужно укниколайь)", "monthly_contribution": "месячный взнос", "principal": "сумма", "annual_rate_percent": "годовая инфляция в %" if params["mode"] == "hypothetical_inflation" else "номинальная годовая ставка в %", "currency": "валюта (рубли, доллары или евро)", "months": "срок в месяцах или годах"}
    missing = [names[k] for k in required if k not in params]
    if missing:
        return {"parameters": params, "clarification": "Для гипотетического расчёта уточните: " + "; ".join(missing) + "."}
    params.setdefault("start", now.date().isoformat())
    if params["mode"] == "hypothetical_goal" and 1 <= params["months"] <= 600:
        start = date.fromisoformat(params["start"])
        year, month = divmod(start.year * 12 + start.month - 1 + params["months"], 12)
        params["deadline"] = date(year, month + 1, min(start.day, monthrange(year, month + 1)[1])).isoformat()
    try:
        scenario = ChatScenario.model_validate(params)
    except ValueError:
        return {"parameters": {}, "clarification": "Параметры вне допустимых границ. Срок: 1–600 месяцев; суммы неотрицательные, ставка: 0–100%."}
    return {"parameters": params, "scenario": scenario}
