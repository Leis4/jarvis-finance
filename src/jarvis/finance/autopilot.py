"""Qwen autopilot: automatic classification and goal-aware monthly budget plans.

Money is always computed here with Decimal; the model only explains and can ask
for these deterministic plans. Every automatic write is audited and reversible.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from jarvis.finance.categories import CATEGORY_BY_KEY
from jarvis.finance.event_taxonomy import (
    EXPENSE_CATEGORIES,
    NON_ECONOMIC_CATEGORIES,
    UNRESOLVED_CATEGORIES,
    canonical_category,
    category_title,
)
from jarvis.finance.personal_cfo import PersonalCFO, money
from jarvis.finance.personal_profile import business_date

SETTINGS_KEY = "cfo.qwen.autopilot"
DEFAULTS = {
    "classify": True,
    "budget": True,
    "classify_limit": 3,
}
# Cut intensity per bucket; essentials are never reduced by the planner.
BUCKET_CUT = {"wants": Decimal("0.22"), "flex": Decimal("0.14"), "semi": Decimal("0.08"),
              "debt": Decimal(0), "goal": Decimal(0), "essential": Decimal(0)}
CENT = Decimal("0.01")


def load_settings(cfo: PersonalCFO) -> dict[str, Any]:
    stored = cfo.db.get_setting(SETTINGS_KEY, {}) or {}
    merged = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    merged["classify"] = bool(merged["classify"])
    merged["budget"] = bool(merged["budget"])
    try:
        merged["classify_limit"] = max(1, min(5, int(merged["classify_limit"])))
    except (TypeError, ValueError):
        merged["classify_limit"] = DEFAULTS["classify_limit"]
    return merged


def save_settings(cfo: PersonalCFO, patch: dict[str, Any]) -> dict[str, Any]:
    current = load_settings(cfo)
    if "classify" in patch:
        current["classify"] = bool(patch["classify"])
    if "budget" in patch:
        current["budget"] = bool(patch["budget"])
    if "classify_limit" in patch:
        current["classify_limit"] = max(1, min(5, int(patch["classify_limit"])))
    cfo.db.set_setting(SETTINGS_KEY, current)
    return current


def unknown_transactions(cfo: PersonalCFO, limit: int = 50) -> list[dict[str, Any]]:
    rows = cfo.economic_rows(cfo.db.all_transactions())
    unknown = [row for row in rows
               if canonical_category(str(row["category"])) in UNRESOLVED_CATEGORIES]
    unknown.sort(key=lambda row: (row["tx_date"], row["id"]), reverse=True)
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in unknown:
        key = f"{row['merchant']}|{row['description']}|{'.' if money(row['amount']) < 0 else '+'}"
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
        if len(result) >= limit:
            break
    return result


def apply_auto_category(cfo: PersonalCFO, tx_id: int, category: str,
                        *, reason: str = "", source: str = "qwen_auto") -> bool:
    """Apply one automatic category with a versioned, audited correction row."""
    from jarvis.finance.event_taxonomy import TAXONOMY_VERSION

    with cfo.db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT category FROM transactions WHERE id=?", (tx_id,)).fetchone()
        if row is None:
            return False
        version = conn.execute(
            "SELECT COALESCE(MAX(version),0) FROM category_corrections WHERE transaction_id=?",
            (tx_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO category_corrections(transaction_id,version,old_category,new_category,source,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (tx_id, version + 1, row["category"], category, source, datetime.now(UTC).isoformat()))
        conn.execute("UPDATE transactions SET category=? WHERE id=?", (category, tx_id))
        cfo.db.audit(conn, "category_auto_applied", {
            "tx_id": tx_id, "source": source, "version": version + 1,
            "taxonomy_version": TAXONOMY_VERSION, "reason": reason[:80]})
    return True


async def classify_pending(cfo: PersonalCFO, *, limit: int | None = None,
                           force: bool = False, cooldown_minutes: int = 10) -> dict[str, Any]:
    """Classify unknown operations with Qwen, applying only when the owner allows it."""
    from jarvis.finance.classification.intelligence import propose, screened
    from jarvis.finance.classification.learning import neighbor_proposal

    settings = load_settings(cfo)
    if not force:
        last = cfo.db.get_setting("cfo.qwen.last_autopilot", None) or {}
        at = last.get("at")
        if at:
            try:
                elapsed = (datetime.now(UTC) - datetime.fromisoformat(at)).total_seconds()
            except (TypeError, ValueError):
                elapsed = cooldown_minutes * 60
            if elapsed < cooldown_minutes * 60:
                return {"status": "cooldown", "processed": 0, "applied": 0, "items": [],
                        "retry_after_seconds": int(cooldown_minutes * 60 - elapsed)}
    if not settings["classify"]:
        return {"status": "disabled", "processed": 0, "applied": 0, "items": []}
    budget = max(1, min(5, int(limit or settings["classify_limit"])))
    rows = unknown_transactions(cfo, limit=budget)
    items: list[dict[str, Any]] = []
    applied = 0
    cost = Decimal(0)
    for row in rows:
        tx_id = int(row["id"])
        cached = cfo.db.get_setting(f"cfo.ai_proposal:{tx_id}", {}) or {}
        proposal = cached.get("result") or {}
        origin = "cached"
        if not proposal or proposal.get("category") in {None, "", "unknown"}:
            local = neighbor_proposal(cfo, tx_id)
            if local is not None:
                proposal = local.get("result") or {}
                origin = "local_history"
            else:
                features = screened(row["description"], money(row["amount"]),
                                    cfo.confirmed_categories(row["merchant"], money(row["amount"])))
                result = await propose(features)
                proposal = result.get("result") or {}
                origin = "qwen"
                cost += Decimal(str(result.get("paid_cost_rub") or "0"))
                cfo.db.set_setting(f"cfo.ai_proposal:{tx_id}", result)
        category = canonical_category(str(proposal.get("category") or ""))
        confidence = proposal.get("confidence")
        accepted = (category in EXPENSE_CATEGORIES or category in NON_ECONOMIC_CATEGORIES
                    or category in {"refund", "parent_transfer_in", "sibling_transfer_in",
                                    "friend_transfer_in", "friend_reimbursement", "family_income",
                                    "parent_transfer_out", "sibling_transfer_out",
                                    "friend_transfer_out", "savings_transfer", "invest_kopilka"})
        applied_now = False
        if accepted and category not in UNRESOLVED_CATEGORIES:
            applied_now = apply_auto_category(cfo, tx_id, category,
                reason=str(proposal.get("reason_text") or proposal.get("reason") or origin))
            applied += int(applied_now)
        items.append({
            "id": tx_id, "merchant": row["merchant"][:60], "amount": str(row["amount"]),
            "suggested": category or None, "origin": origin, "applied": applied_now,
            "confidence": confidence if isinstance(confidence, (int, float)) else None,
            "reason": str(proposal.get("reason_text") or "")[:140],
        })
    if applied:
        cfo.db.set_setting("cfo.qwen.last_autopilot", {
            "at": datetime.now(UTC).isoformat(), "applied": applied})
    return {"status": "ok" if items else "empty", "processed": len(items),
            "applied": applied, "paid_cost_rub": str(cost), "items": items}


def _monthly_average(cfo: PersonalCFO, days: int = 60) -> dict[str, Decimal]:
    today = business_date(datetime.now(UTC))
    rows = cfo.economic_rows(cfo.db.transactions_between(today - timedelta(days=days), today))
    totals: dict[str, Decimal] = {}
    for row in rows:
        category = canonical_category(str(row["category"]))
        amount = money(row["amount"])
        if amount >= 0 or category in NON_ECONOMIC_CATEGORIES:
            continue
        if category in UNRESOLVED_CATEGORIES:
            category = "unknown"
        totals[category] = totals.get(category, Decimal(0)) + abs(amount)
    months = Decimal(days) / Decimal(30)
    return {key: (value / months).quantize(CENT, rounding=ROUND_HALF_UP)
            for key, value in totals.items()}


def budget_plan(cfo: PersonalCFO, *, goal_id: int | None = None) -> dict[str, Any]:
    """Deterministic monthly limits that keep essentials and fund the goal."""
    today = business_date(datetime.now(UTC))
    average = _monthly_average(cfo)
    position = cfo.dashboard()["financial_position"]
    goals = [goal for goal in cfo.goals(today) if goal.get("status") == "active"]
    selected = next((goal for goal in goals if goal_id and int(goal["id"]) == goal_id), None)
    if selected is None and goals:
        selected = goals[0]
    goal_monthly = Decimal(0)
    goal_note = "Цель не выбрана — план просто повторяет ваши средние траты."
    if selected:
        remaining = max(Decimal(0), money(selected["target_amount"]) - money(selected["current_amount"]))
        deadline = selected.get("deadline")
        if deadline:
            months_left = max(1, (int(deadline[5:7]) - today.month) + 12 * (int(deadline[:4]) - today.year))
        else:
            months_left = 6
        goal_monthly = (remaining / Decimal(months_left)).quantize(CENT, rounding=ROUND_CEILING)
        goal_note = (f"Цель «{selected['name']}»: осталось {remaining} ₽ за {months_left} мес. "
                     f"нужно около {goal_monthly} ₽ в месяц.")

    categories: list[dict[str, Any]] = []
    flexible_total = Decimal(0)
    planned_total = Decimal(0)
    for category, avg in sorted(average.items(), key=lambda item: item[1], reverse=True):
        if category == "unknown":
            continue
        definition = CATEGORY_BY_KEY.get(category)
        bucket = definition.bucket if definition else "flex"
        necessity = definition.necessity if definition else "flex"
        if necessity == "transfer":
            # Transfers to people and between accounts are not a budget line.
            continue
        cut = BUCKET_CUT.get(necessity, Decimal(0))
        suggested = (avg * (1 - cut)).quantize(CENT, rounding=ROUND_HALF_UP)
        if necessity in {"flex", "wants", "semi"}:
            flexible_total += avg - suggested
        planned_total += suggested
        categories.append({
            "category": category, "title": category_title(category), "bucket": bucket,
            "necessity": necessity, "average": str(avg), "suggested": str(suggested),
            "cut_percent": int((cut * 100).to_integral_value()),
        })

    months_to_goal = None
    if selected and goal_monthly > 0:
        remaining = max(Decimal(0), money(selected["target_amount"]) - money(selected["current_amount"]))
        capacity = flexible_total
        months_to_goal = (max(1, int((remaining / capacity).to_integral_value(rounding=ROUND_CEILING)))
                          if capacity > 0 else None)
    headroom = position.get("headroom")
    fits = headroom is not None and planned_total <= money(headroom) + planned_total
    return {
        "status": "available" if categories else "insufficient_data",
        "month": today.strftime("%Y-%m"),
        "categories": categories,
        "planned_total": str(planned_total),
        "flexible_freed": str(flexible_total),
        "goal": ({"id": selected["id"], "name": selected["name"],
                  "monthly_need": str(goal_monthly), "months_to_goal": months_to_goal}
                 if selected else None),
        "goal_note": goal_note,
        "headroom": headroom,
        "fits_headroom": bool(fits),
        "note": "Лимиты — предложение. Обязательные траты не урезаются, гибкие — на 8–22%.",
        "financial_apply": False,
    }


def apply_budget_plan(cfo: PersonalCFO, categories: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist the owner-confirmed plan as the monthly limits."""
    limits: dict[str, str] = {}
    for item in categories[:40]:
        category = canonical_category(str(item.get("category") or ""))
        if category not in CATEGORY_BY_KEY and category not in EXPENSE_CATEGORIES:
            continue
        amount = money(item.get("suggested") or item.get("limit") or "0")
        if amount < 0 or amount > Decimal(10000000):
            continue
        limits[category] = str(amount.quantize(CENT))
    if not limits:
        return {"status": "empty", "applied": 0}
    with cfo.db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     ("cfo.category_limits", __import__("json").dumps(limits, ensure_ascii=False)))
        cfo.db.audit(conn, "budget_plan_applied", {"categories": len(limits), "source": "qwen_plan"})
    return {"status": "applied", "applied": len(limits), "limits": limits}
