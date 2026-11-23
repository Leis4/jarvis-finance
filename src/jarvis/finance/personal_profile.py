"""Dated owner assumptions and a Decimal cash calendar using existing finance tools."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jarvis.finance.control import capacity_scenarios
from jarvis.finance.goals import project_savings_goal
from jarvis.finance.ledger import cashflow, conflicting_identities, finite_money

CENT = Decimal("0.01")


def business_date(moment: datetime) -> date:
    """Use Moscow dates for bank calendars and UTC for recorded instants."""
    if moment.tzinfo is None:
        raise ValueError('Aware financial timestamp required')
    return moment.astimezone(ZoneInfo('Europe/Moscow')).date()


def exact_money(value: Any) -> Decimal:
    """Validate existing Decimal money without silently rounding user input."""
    amount = finite_money(value)
    if isinstance(value, bool) or abs(amount) > Decimal("1e12") or amount != amount.quantize(CENT):
        raise ValueError("Требуется денежная сумма с точностью до копейки.")
    return amount


class OwnerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AmountFact(OwnerModel):
    low: str
    high: str | None = None
    source: Literal["owner_confirmed", "owner_estimate"] = "owner_estimate"
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def checked(self) -> AmountFact:
        low, high = exact_money(self.low), exact_money(self.high if self.high is not None else self.low)
        if low < 0 or high < low or (self.updated_at is not None and self.updated_at.tzinfo is None):
            raise ValueError("Неверный диапазон или время оценки.")
        return self


class Asset(AmountFact):
    name: str = Field(min_length=1, max_length=100)
    liquid: bool = False


class CalendarItem(OwnerModel):
    name: str = Field(min_length=1, max_length=100)
    amount: str
    category: str = Field(default="other", max_length=80)
    frequency: Literal["once", "daily", "weekly", "monthly", "interval"] = "once"
    interval_days: int = Field(default=14, ge=1, le=366)
    next_date: date | None = None
    latest_date: date | None = None
    certainty: Literal["confirmed", "expected", "estimate", "candidate"] = "expected"
    enabled: bool = True
    updated_at: datetime | None = None
    source: Literal["owner_confirmed", "owner_estimate"] = "owner_estimate"

    @model_validator(mode="after")
    def checked(self) -> CalendarItem:
        if exact_money(self.amount) < 0 or (self.latest_date and (not self.next_date or self.latest_date < self.next_date)):
            raise ValueError("Неверная сумма или диапазон даты.")
        if self.updated_at is not None and self.updated_at.tzinfo is None:
            raise ValueError("Требуется время с timezone.")
        return self


class PersonalProfile(OwnerModel):
    version: Literal[2] = 2
    cash: AmountFact | None = None
    other_assets: list[Asset] = Field(default_factory=list, max_length=30)
    protected: AmountFact | None = None
    reserve: AmountFact | None = None
    incomes: list[CalendarItem] = Field(default_factory=list, max_length=50)
    obligations: list[CalendarItem] = Field(default_factory=list, max_length=100)
    obligations_complete: bool = False
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def unique_names(self) -> PersonalProfile:
        for values in (self.other_assets, self.incomes, self.obligations):
            names = [v.name.casefold() for v in values]
            if len(set(names)) != len(names):
                raise ValueError("Названия внутри списка должны быть уникальны.")
        return self

    def stamped(self, now: datetime, previous: PersonalProfile | None = None) -> PersonalProfile:
        """Stamp submitted values, preserving explicit source/uncertainty."""
        data = self.model_dump(mode="json")
        old = previous.model_dump(mode="json") if previous else {}
        data["updated_at"] = now.isoformat()
        def stamp(item: dict[str, Any], prior: dict[str, Any] | None) -> None:
            content = {k: v for k, v in item.items() if k != "updated_at"}
            same = prior is not None and content == {k: v for k, v in prior.items() if k != "updated_at"}
            item["updated_at"] = prior.get("updated_at") if same and prior else now.isoformat()
        for key in ("cash", "protected", "reserve"):
            if data[key] is not None:
                stamp(data[key], old.get(key))
        for key in ("other_assets", "incomes", "obligations"):
            prior = {v["name"]: v for v in old.get(key, [])}
            for item in data[key]:
                stamp(item, prior.get(item["name"]))
        return PersonalProfile.model_validate(data)


class GoalInput(OwnerModel):
    name: str = Field(min_length=1, max_length=100)
    target_amount: str
    current_amount: str = "0.00"
    monthly_contribution: str | None = None
    priority: int = Field(default=3, ge=1, le=10)
    deadline: date | None = None
    status: Literal["active", "paused", "completed"] = "active"

    @model_validator(mode="after")
    def checked(self) -> GoalInput:
        for v in (self.target_amount, self.current_amount, self.monthly_contribution):
            if v is not None and exact_money(v) < 0:
                raise ValueError("Goal amounts must be nonnegative")
        return self


def dates(item: CalendarItem, start: date, end: date) -> list[date]:
    """Expand bounded schedules; missing dates never become today's income."""
    if not item.enabled or item.certainty == "candidate" or item.next_date is None:
        return []
    result = []
    anchor = item.latest_date or item.next_date
    index = 0
    if item.frequency == "monthly":
        index = max(0, (start.year - anchor.year) * 12 + start.month - anchor.month - 1)
        year, month = divmod(anchor.year * 12 + anchor.month - 1 + index, 12)
        current = date(year, month + 1, min(anchor.day, monthrange(year, month + 1)[1]))
    elif item.frequency != "once":
        step = 1 if item.frequency == "daily" else 7 if item.frequency == "weekly" else item.interval_days
        index = max(0, (start - anchor).days // step - 1)
        current = anchor + timedelta(days=index * step)
    else:
        current = anchor
    steps = 0
    while current <= end and steps < 5000:
        if current >= start:
            result.append(current)
        if item.frequency == "once":
            break
        steps += 1
        index += 1
        if item.frequency == "monthly":
            year, month = divmod(anchor.year * 12 + anchor.month - 1 + index, 12)
            current = date(year, month + 1, min(anchor.day, monthrange(year, month + 1)[1]))
        else:
            step = 1 if item.frequency == "daily" else 7 if item.frequency == "weekly" else item.interval_days
            current = anchor + timedelta(days=index * step)
    return result


def goal_view(goal: dict[str, Any], metadata: dict[str, Any], today: date) -> dict[str, Any]:
    deadline = date.fromisoformat(goal["deadline"]) if goal.get("deadline") else None
    monthly = metadata.get("monthly_contribution")
    projection = None
    if deadline is None or deadline >= today:
        try:
            projection = project_savings_goal(goal["target_amount"], goal["current_amount"],
                "0" if goal["status"] != "active" else monthly, start=today,
                months=600, currency="RUB", deadline=deadline)
        except ValueError:
            projection = {"status": "unavailable", "reason": "Goal exceeds supported projection parameters"}
    return {**goal, "monthly_contribution": monthly, "projection": projection,
            "source": metadata.get("source", "existing_goal"), "updated_at": metadata.get("updated_at", goal.get("updated_at"))}


def financial_decision(bank: dict[str, Any] | None, profile: PersonalProfile, rows: list[dict[str, Any]],
                       goals: list[dict[str, Any]], *, now: datetime, price: Decimal = Decimal(0),
                       zero_friction: bool = True) -> dict[str, Any]:
    """Calculate owner-scoped scenarios; owner defaults do not block a known balance."""
    today = business_date(now)
    accounts = (bank or {}).get("accounts", [])
    rub = [a for a in accounts if a["currency"] == "RUB"]
    bank_total = sum((exact_money(a["amount"]) for a in rub), Decimal(0)) if rub else None
    liquid_bank = sum((exact_money(a["amount"]) for a in rub if not a.get("restricted")), Decimal(0)) if rub else None
    cash_low = exact_money(profile.cash.low) if profile.cash else Decimal(0)
    cash_high = exact_money(profile.cash.high or profile.cash.low) if profile.cash else Decimal(0)
    other_low = sum((exact_money(a.low) for a in profile.other_assets), Decimal(0))
    other_high = sum((exact_money(a.high or a.low) for a in profile.other_assets), Decimal(0))
    other_liquid = sum((exact_money(a.low) for a in profile.other_assets if a.liquid), Decimal(0))
    other_liquid_high = sum((exact_money(a.high or a.low) for a in profile.other_assets if a.liquid), Decimal(0))
    has_liquid_source = liquid_bank is not None or profile.cash is not None or bool(other_liquid)
    liquid = ((liquid_bank or Decimal(0)) + cash_low + other_liquid) if has_liquid_source else None
    total_low = ((bank_total or Decimal(0)) + cash_low + other_low) if has_liquid_source else None
    total_high = ((bank_total or Decimal(0)) + cash_high + other_high) if has_liquid_source else None
    reserve_defaulted = profile.reserve is None
    protected_defaulted = profile.protected is None
    reserve = (exact_money(profile.reserve.high or profile.reserve.low)
        if profile.reserve else Decimal(0))
    protected = (exact_money(profile.protected.high or profile.protected.low)
        if profile.protected else Decimal(0))
    missing = []
    blocking_missing = []
    if conflicting_identities(rows) or any(r.get('identity_conflict') for r in rows):
        missing.append('подтверждённая идентичность банковских операций')
        blocking_missing.append('подтверждённая идентичность банковских операций')
    if bank is None or not rub:
        missing.append("актуальные банковские остатки")
        if liquid is None:
            blocking_missing.append("актуальные банковские остатки")
    elif not timedelta(0) <= now - datetime.fromisoformat(bank["observed_at"]) <= timedelta(minutes=15):
        missing.append("свежее банковское наблюдение")
        blocking_missing.append("свежее банковское наблюдение")
    if profile.cash is None:
        missing.append("оценка наличных или явный ноль")
    if reserve_defaulted:
        missing.append("резерв не задан — принят 0")
    if protected_defaulted:
        missing.append("защищённые средства не заданы — приняты 0")
    if not profile.obligations_complete:
        missing.append("список обязательств может быть неполным")
    undated = [i.name for i in profile.incomes if i.enabled and i.next_date is None]
    undated_costs = [i.name for i in profile.obligations if i.enabled and i.certainty != "candidate" and i.next_date is None]
    if undated_costs:
        missing.append("даты обязательств: " + ", ".join(undated_costs))
    upcoming = [(d, i) for i in profile.incomes for d in dates(i, today + timedelta(days=1), today + timedelta(days=366))]
    next_income = min((d for d, _ in upcoming), default=None)
    horizon = max(1, (next_income - today).days) if next_income else 30
    goal_reserved = sum((exact_money(g["current_amount"]) for g in goals if g["status"] in {"active", "paused"}), Decimal(0))
    if not upcoming:
        missing.append("дата следующего поступления")
    # Ordinary history is only a provisional rate; explicit recurring expense
    # categories are excluded so food/transport cannot be charged twice.
    covered_categories = {i.category for i in profile.obligations if i.enabled and i.certainty != "candidate"}
    from jarvis.finance.event_taxonomy import NON_ECONOMIC_CATEGORIES
    eligible = []
    for row in rows:
        d = date.fromisoformat(row["tx_date"])
        if (today - timedelta(days=89) <= d <= today and row["currency"] == "RUB"
                and row["category"] not in NON_ECONOMIC_CATEGORIES | covered_categories):
            eligible.append(row)
    flow = cashflow(eligible)
    rate = max(Decimal(0), flow["expenses"] + flow['unresolved_outflow']) / Decimal(90)
    # Scenario stress, not calibrated probability or invented accuracy.
    rates = {"pessimistic": rate * Decimal("1.5"), "base": rate, "optimistic": rate * Decimal("0.5")}
    future = []
    def path(days: int, scenario: str, buy_day: int | None) -> dict[str, Any]:
        balance = liquid
        if balance is not None and scenario == "optimistic":
            balance += cash_high - cash_low + other_liquid_high - other_liquid
        minimum = balance
        inflow, costs = Decimal(0), Decimal(0)
        goal_cost = Decimal(0)
        for offset in range(days + 1):
            day = today + timedelta(days=offset)
            for item in profile.incomes:
                if offset > 0 and day in dates(item, day, day) and (scenario != "pessimistic" or item.certainty == "confirmed"):
                    inflow += exact_money(item.amount)
                    if balance is not None:
                        balance += exact_money(item.amount)
            for item in profile.obligations:
                if day in dates(item, day, day):
                    costs += exact_money(item.amount)
                    if balance is not None:
                        balance -= exact_money(item.amount)
            if offset > 0:
                costs += rates[scenario]
                if balance is not None:
                    balance -= rates[scenario]
            for goal in goals:
                goal_day = min(today.day, monthrange(day.year, day.month)[1])
                if goal["status"] == "active" and goal.get("monthly_contribution") and offset > 0 and day.day == goal_day:
                    value = exact_money(goal["monthly_contribution"])
                    goal_cost += value
                    if balance is not None:
                        balance -= value
            if offset == buy_day and balance is not None:
                balance -= price
            if balance is not None:
                minimum = min(minimum if minimum is not None else balance, balance)
        safe = None if minimum is None else minimum - reserve - protected - goal_reserved
        return {"closing": str(balance.quantize(CENT, rounding=ROUND_FLOOR)) if balance is not None else None,
            "minimum": str(minimum.quantize(CENT, rounding=ROUND_FLOOR)) if minimum is not None else None,
            "headroom": str(safe.quantize(CENT, rounding=ROUND_FLOOR)) if safe is not None else None,
            "scheduled_income": str(inflow), "known_expenses_and_baseline": str(costs.quantize(CENT)),
            "goal_contributions": str(goal_cost), "obligations_complete": profile.obligations_complete}
    for days in (1, 7, 14, 30):
        future.append({"days": days, "kind": "conditional_scenario", "variants": {
            name: path(days, name, None) for name in rates}})
    # Compare all purchase choices on the same horizon, at least through receipt.
    comparison_days = max(30, horizon)
    scenarios: dict[str, Any] = {}
    for name, when in (("buy_now", 0), ("wait_for_income", horizon if next_income else None), ("keep_money", None)):
        scenarios[name] = {"buy_date": (today + timedelta(days=when)).isoformat() if when is not None else None,
            "available": name != "wait_for_income" or next_income is not None,
            "variants": {s: path(comparison_days, s, when if name != "keep_money" else None) for s in rates}}
    # Reuse source-bound capacity math for deterministic protected headroom.
    planning = identity_profile(profile)
    components = [{"account": a["account"], "amount": a["amount"], "currency": "RUB", "coverage_confirmed": True,
        "liquid": True, "as_of": bank["observed_at"], "source_sha256": (bank.get("capture_sha256") or planning)}
        for a in rub if not a.get("restricted")] if bank else []
    if profile.cash:
        components.append({"account": "owner_cash", "amount": profile.cash.low, "currency": "RUB", "coverage_confirmed": True,
            "liquid": True, "as_of": now.isoformat(), "source_sha256": planning})
    for index, asset in enumerate(profile.other_assets):
        if asset.liquid:
            components.append({"account": f"owner_asset_{index}", "amount": asset.low, "currency": "RUB", "coverage_confirmed": True,
                "liquid": True, "as_of": now.isoformat(), "source_sha256": planning})
    due = sum((exact_money(i.amount) * len(dates(i, today, today + timedelta(days=horizon))) for i in profile.obligations), Decimal(0))
    simple_headroom = (liquid - due - reserve - protected - goal_reserved
        if liquid is not None else None)
    capacity = capacity_scenarios(components, frozenset(str(c["account"]) for c in components), "RUB",
        due, reserve + protected + goal_reserved,
        {"buy_now": price, "keep_money": Decimal(0)}, obligations_confirmed=profile.obligations_complete,
        reserve_confirmed=True, planning_source_sha256=planning, now=now)
    if capacity.get("status") != "experimental" and simple_headroom is not None:
        capacity = {**capacity, "fallback": {
            "headroom": str(simple_headroom.quantize(CENT, rounding=ROUND_FLOOR)),
            "basis": "known balance minus known obligations and effective reserve; incomplete owner data does not block the estimate",
        }}
    worst = scenarios["buy_now"]["variants"]["pessimistic"]["headroom"]
    base = scenarios["buy_now"]["variants"]["base"]["headroom"]
    recommendation = ("insufficient_data" if blocking_missing else "risk_shortfall" if base is None or Decimal(base) < 0 else
                      "wait" if worst is None or Decimal(worst) < 0 else "can_buy")
    return {"status": recommendation, "total_low": str(total_low) if total_low is not None else None,
        "total_high": str(total_high) if total_high is not None else None, "liquid": str(liquid) if liquid is not None else None,
        "headroom": str(simple_headroom.quantize(CENT, rounding=ROUND_FLOOR)) if simple_headroom is not None else None,
        "blocking_missing": blocking_missing,
        "bank_total": str(bank_total) if bank_total is not None else None,
        "reserve": str(reserve) if not reserve_defaulted else None,
        "protected": str(protected) if not protected_defaulted else None,
        "reserve_effective": str(reserve), "protected_effective": str(protected),
        "reserve_defaulted": reserve_defaulted, "protected_defaulted": protected_defaulted,
        "goal_reserved": str(goal_reserved), "horizon_days": horizon,
        "known_obligations_until_next_income": str(due), "obligations_complete": profile.obligations_complete,
        "after_purchase_low": str(liquid - price) if liquid is not None else None,
        "after_purchase_high": str(liquid + cash_high - cash_low + other_liquid_high - other_liquid - price) if liquid is not None else None,
        "missing": missing, "undated_incomes": undated, "next_income": next_income.isoformat() if next_income else None,
        "forecasts": future, "scenarios": scenarios, "capacity": capacity, "goals": goals,
        "until_next_income": {s: path(horizon, s, None) for s in rates},
        "probability_shortfall": None, "probability_reason": "Independent, complete backtested history unavailable; stress scenarios only",
        "assumptions": ["Historical residual spending / 90 calendar days; incomplete history can understate costs.",
            "Pessimistic income includes confirmed only; expected/uncertain receipts are not guaranteed.",
            "Expense baseline multipliers 1.5 / 1 / 0.5 are explicit stress assumptions, not confidence intervals."],
        "profile_updated_at": profile.updated_at.isoformat() if profile.updated_at else None}


def identity_profile(profile: PersonalProfile) -> str:
    import hashlib
    return hashlib.sha256(profile.model_dump_json().encode()).hexdigest()
