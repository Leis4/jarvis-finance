"""Explain observed money, missing coverage and chronological forecast validation."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import statistics
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from math import isfinite
from pathlib import Path
from sys import float_info
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from jarvis.finance.review_store import ReviewStore

from jarvis.finance.db import FinanceDB
from jarvis.finance.ledger import cashflow, reconcile
from jarvis.finance.rules import category_statuses
from jarvis.finance.source_health import source_health


class AcceptedStatementScope(BaseModel):
    """Explicit narrow evidence scope; never grants full-ledger readiness."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    start: date
    end: date
    accounts: tuple[str, ...] | None = None
    expected_version: str | None = Field(default=None, pattern='^[0-9a-f]{64}$')


def _category_comparison(current: dict[str, Decimal], previous: dict[str, Decimal], *,
                         current_days: int, previous_days: int, currency: str) -> dict[str, Any]:
    """Compare accepted net expenses; day normalization is descriptive, not causal."""
    categories = []
    with localcontext() as context:
        context.prec = 40
        context.rounding = ROUND_HALF_EVEN
        for category in sorted(current.keys() | previous.keys()):
            now, prior = current.get(category, Decimal(0)), previous.get(category, Decimal(0))
            now_daily, prior_daily = now / current_days, prior / previous_days
            delta = now_daily - prior_daily
            categories.append({"category": category, "current": str(now), "previous": str(prior),
                "change": str(now - prior), "current_daily": str(now_daily.quantize(Decimal("0.000001"))),
                "previous_daily": str(prior_daily.quantize(Decimal("0.000001"))),
                "daily_change": str(delta.quantize(Decimal("0.000001"))),
                "daily_direction": "increase" if delta > 0 else "decrease" if delta < 0 else "unchanged"})
    return {"current_days": current_days, "previous_days": previous_days,
        "equal_duration": current_days == previous_days, "currency": currency, "units": currency + "/calendar_day",
        "formula": "accepted ledger net category expense / inclusive calendar days; current daily rate minus previous daily rate",
        "rounding": "half-even six decimal places for display; direction from unrounded Decimal rates",
        "causality_verified": False, "categories": categories}


def financial_question(path: Path | None, *, month: str = '', intent: str = "spending", currency: str = "RUB",
                       accepted_sources: frozenset[str] = frozenset(), accepted_sha256: str | None = None,
                       now: datetime | None = None, question: str | None = None,
                       statement_scope: AcceptedStatementScope | None = None,
                       review_store: ReviewStore | None = None) -> dict[str, Any]:
    """Answer scoped questions from a read-only ledger snapshot, never from an LLM."""
    from jarvis.finance.event_taxonomy import canonical_category
    from jarvis.finance.readiness import readonly_snapshot

    if statement_scope is not None:
        from jarvis.finance.statement_product import StatementProduct
        if path is not None or review_store is None or question is not None:
            raise ValueError('Use one explicit statement authority and typed intent')
        return StatementProduct(review_store).question(statement_scope, intent=intent, currency=currency, now=now)
    if path is None:
        raise ValueError('Explicit accepted ledger or statement scope required')

    current = now or datetime.now(UTC)
    if question is not None:
        phrases = {"сколько потрачено": "spending", "сколько я потратил": "spending",
            "почему траты на продукты выросли": "category_growth",
            "почему ты не показываешь свободные деньги": "available_funds",
            "сколько я могу откладывать": "available_funds",
            "какие категории выросли": "category_growth", "какие расходы выросли": "category_growth",
            "какие подписки повторяются": "recurring", "есть ли дубли": "duplicates",
            "история расходов": "expense_history",
            "каких данных не хватает": "missing_data", "насколько свежие остатки": "stale_balances",
            "сколько свободных средств": "available_funds"}
        normalized = " ".join(question.casefold().strip().rstrip("?.!").split())
        if normalized.endswith(" за прошлый месяц"):
            if month != (current.date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m"):
                return {"status": "insufficient_data", "answer": None, "reason": "Выбранный период не соответствует прошлому месяцу",
                        "database_modified": False, "apply_allowed": False}
            normalized = normalized.removesuffix(" за прошлый месяц")
        if normalized not in phrases:
            return {"status": "insufficient_data", "answer": None, "reason": "Вопрос не поддержан: выберите вопрос и период",
                    "database_modified": False, "apply_allowed": False}
        intent = phrases[normalized]
    intents = {"spending", "expense_history", "category_growth", "recurring", "duplicates", "missing_data", "stale_balances", "available_funds"}
    if (current.tzinfo is None or intent not in intents or not isinstance(month, str) or len(month) != 7
            or len(currency) != 3 or not currency.isascii() or not currency.isalpha() or not currency.isupper()):
        raise ValueError("Explicit question, month, currency and aware clock required")
    start = date.fromisoformat(month + "-01")
    next_month = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    end = next_month - timedelta(days=1)
    if end >= current.date():
        raise ValueError("Closed historical month required")
    result: dict[str, Any] = {"intent": intent, "status": "unavailable", "answer": None,
        "period": [start.isoformat(), end.isoformat()], "currency": currency, "checked_at": current.isoformat(),
        "source_sha256": None, "source_refs": [], "formula": None, "excluded_rows": [],
        "acceptance_basis": "explicit_snapshot_hash_and_source_scope_not_bank_authentication",
        "bank_authenticity_verified": False, "apply_allowed": False, "database_modified": False}
    result["explanation"] = {"spending": "Расходы за выбранный месяц за вычетом возвратов; переводы и капитал исключены",
        "expense_history": "Принятые расходы и возвраты с происхождением; категории считает ledger, не модель",
        "category_growth": "Разница принятых расходов по категориям с предыдущим календарным месяцем",
        "recurring": "Повторяющиеся подписки — кандидаты для проверки, не подтверждённые обязательства",
        "duplicates": "Повторяющиеся банковские идентификаторы; автоматическое объединение запрещено",
        "missing_data": "Пробелы покрытия, происхождения операций и остатков",
        "stale_balances": "Исторические остатки не подтверждают текущий баланс",
        "available_funds": "Нужны отдельно принятые текущие остатки, резерв и обязательства"}[intent]
    if intent == "category_growth":
        result["causality_verified"] = False
    try:
        if path.is_symlink():
            raise ValueError("Stable original SQLite required")
        with readonly_snapshot(path) as (copy, sha):
            result.update(source_sha256=sha, snapshot_version="ledger-question-v2",
                          source_identity=hashlib.sha256(str(path.resolve()).encode()).hexdigest())
            with closing(sqlite3.connect(copy)) as original:
                result["audit_revision"] = original.execute("SELECT coalesce(max(id),0) FROM audit_log").fetchone()[0]
                result["audit_sha256"] = hashlib.sha256(json.dumps(original.execute(
                    "SELECT id,event_type,payload,created_at FROM audit_log ORDER BY id").fetchall(),
                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            snapshot_accepted = (isinstance(accepted_sha256, str) and len(accepted_sha256) == 64
                                 and accepted_sha256 == sha)
            result["snapshot_accepted"] = snapshot_accepted
            if accepted_sha256 is not None and not snapshot_accepted:
                result.update(status="unavailable", reason="Accepted snapshot changed; explicit acceptance required")
                return result
            db = FinanceDB(copy)
            report = build_audit_report(db, start, end, currency=currency, now=current)
            result.update(source_sha256=sha, source_refs=report["lineage"]["source_event_ids"],
                          coverage=report["status"], unresolved=report["pending_review"])
            all_rows = db.all_transactions()
            with db.connect() as conn:
                accepted_ids = {r[0] for r in conn.execute("SELECT transaction_id FROM source_events WHERE status='accepted'")}
            rows = [r for r in all_rows if r["currency"] == currency and start.isoformat() <= r["tx_date"] <= end.isoformat()]

            def accepted(body: dict[str, Any]) -> bool:
                sources = set(body["sources"]) | {a["source"] for a in body["accounts"] if a.get("source")}
                states = source_health(db, sorted(sources), current)
                ids = set(body["lineage"]["transaction_ids"])
                return (snapshot_accepted and body["status"] == "complete" and bool(sources) and sources <= accepted_sources
                    and ids <= accepted_ids
                    and all(s["availability_status"] == "available" for s in states.values())
                    and not any(canonical_category(str(p["category"])) in {"pending_review", "uncategorized", "unknown", "other"}
                        for r in all_rows if r["id"] in ids for p in r.get("allocations", []) or [r]))

            healthy = accepted(report)
            observed_sources = set(report["sources"]) | {a["source"] for a in report["accounts"] if a.get("source")}
            availability = source_health(db, sorted(observed_sources), current)
            result["source_availability"] = {s: v["availability_status"] for s, v in availability.items()}
            states = set(result["source_availability"].values())
            result["status"] = ("unavailable" if "unavailable" in states else "blocked" if "blocked" in states
                else "stale" if "stale" in states else "available" if healthy else "insufficient_data")
            if intent == "missing_data":
                result["answer"] = {"incomplete_accounts": [a["account"] for a in report["accounts"] if not a["coverage_complete"]],
                    "unknown_balances": [a["account"] for a in report["accounts"] if a["reconciliation"]["closing"] is None],
                    "missing_lineage": report["lineage"]["missing_transaction_ids"],
                    "pending_source_events": report["pending_review"], "accepted_source_scope": healthy}
            elif intent == "stale_balances":
                result["status"] = "stale" if end < current.date() else "insufficient_data"
                result["answer"] = {"asof": end.isoformat(), "current_balance_verified": False,
                    "reason": "Historical closing balances are not current available funds"}
            elif intent == "duplicates":
                groups: dict[tuple[str, str], list[int]] = {}
                for row in rows:
                    if row.get("external_id"):
                        groups.setdefault((row["account"], row["external_id"]), []).append(row["id"])
                result["answer"] = {"observed_duplicate_groups": [ids for ids in groups.values() if len(ids) > 1],
                    "absence_verified": healthy, "automatic_merge_allowed": False}
            elif intent == "available_funds":
                result.update(status="insufficient_data", reason="Use accepted current balances/reserve/obligations via capacity-preview")
            elif healthy:
                flow = cashflow(rows, currency)
                result["excluded_rows"] = [r["id"] for r in rows if cashflow([r], currency)["excluded_movement"] != 0]
                if intent == "spending":
                    result.update(answer=str(flow["expenses"]), formula="economic debits minus refunds; principal/transfers excluded")
                elif intent == "expense_history":
                    result.update(answer={"operations": [{"transaction_id": r["id"], "date": r["tx_date"],
                        "signed_amount": r["amount"], "currency": r["currency"], "category": r["category"],
                        "merchant_identity": hashlib.sha256(" ".join(str(r.get("merchant") or "").casefold().split()).encode()).hexdigest() if r.get("merchant") else None,
                        "allocations": [{"category": p["category"], "amount": p["amount"]} for p in r.get("allocations", [])],
                        "net_expense": str(cashflow([r], currency)["expenses"])}
                        for r in rows if cashflow([r], currency)["expenses"] != 0],
                        "by_category": [{"category": c, "net_expense": str(v)} for c, v in sorted(flow["by_category"].items())],
                        "chart_max": str(max([Decimal(1), *flow["by_category"].values()]))},
                        formula="ledger net expenses by category; refunds retained; transfers/principal excluded")
                elif intent == "category_growth":
                    previous_end = start - timedelta(days=1)
                    previous_start = previous_end.replace(day=1)
                    previous = build_audit_report(db, previous_start, previous_end, currency=currency, now=current)
                    if not accepted(previous):
                        result.update(status="insufficient_data", reason="Previous calendar month unaccepted/incomplete")
                    else:
                        result["source_refs"] = sorted(set(result["source_refs"]) | set(previous["lineage"]["source_event_ids"]))
                        result["comparison_period"] = [previous_start.isoformat(), previous_end.isoformat()]
                        prior_rows = [r for r in all_rows if r["currency"] == currency and previous_start.isoformat() <= r["tx_date"] <= previous_end.isoformat()]
                        prior = cashflow(prior_rows, currency)["by_category"]
                        result["answer"] = [{"category": c, "previous": str(prior.get(c, Decimal(0))),
                            "current": str(v), "change": str(v - prior.get(c, Decimal(0)))}
                            for c, v in sorted(flow["by_category"].items()) if v > prior.get(c, Decimal(0))]
                        result["formula"] = "current calendar month net category expense minus previous calendar month"
                        result["period_comparison"] = _category_comparison(flow["by_category"], prior,
                            current_days=(end - start).days + 1,
                            previous_days=(previous_end - previous_start).days + 1, currency=currency)
                else:
                    subscriptions: dict[str, list[dict[str, Any]]] = {}
                    for row in rows:
                        if row["category"] == "subscriptions" and Decimal(row["amount"]) < 0:
                            subscriptions.setdefault(str(row["merchant"]), []).append(row)
                    result.update(answer=[{"transaction_ids": [r["id"] for r in group],
                        "amount_total": str(-sum((Decimal(r["amount"]) for r in group), Decimal(0))),
                        "status": "candidate_requires_confirmation"} for group in subscriptions.values()
                        if len({r["tx_date"] for r in group}) >= 2],
                        formula="repeated negative subscription rows in requested month; candidates require human confirmation")
            if not healthy and "reason" not in result:
                result["reason"] = "Source scope, freshness, reconciliation or economic review incomplete"
            result["calculation_id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, ArithmeticError):
        result.update(status="unavailable", answer=None, reason="Ledger evidence unavailable or malformed")
    return result


def liquidity_forecast(daily: list[float], balance: Decimal | None, horizon: int = 14, seed: int = 42) -> dict[str, Any]:
    """Compare trailing and weekly seasonal baselines using rolling origin."""
    if type(horizon) is not int or not 1 <= horizon <= 366:
        raise ValueError("horizon must be an integer between 1 and 366")
    if (len(daily) > 10000
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or abs(value) > float_info.max or not isfinite(value) for value in daily)
            or (balance is not None and (not isinstance(balance, Decimal) or not balance.is_finite()))):
        return {"status": "unavailable", "reason": "invalid_forecast_input"}
    if balance is None or len(daily) < 28:
        return {"status": "insufficient_data", "history_days": len(daily), "balance_known": balance is not None}
    daily = [float(value) for value in daily]
    errors: dict[str, list[float]] = {"trailing_7": [], "seasonal_7": []}
    for i in range(14, len(daily)):
        errors["trailing_7"].append(abs(statistics.mean(daily[i - 7:i]) - daily[i]))
        errors["seasonal_7"].append(abs(daily[i - 7] - daily[i]))
    scores = {name: statistics.mean(values) for name, values in errors.items()}
    selected = min(scores, key=lambda name: scores[name])
    trend = [statistics.mean(daily[-7:])] * horizon if selected == "trailing_7" else [daily[-7:][i % 7] for i in range(horizon)]
    rng = random.Random(seed)
    # Empirical uncertainty, not a calibrated probabilistic guarantee.
    residuals = [daily[i] - (statistics.mean(daily[i - 7:i]) if selected == "trailing_7" else daily[i - 7]) for i in range(14, len(daily))]
    projected = float(balance) + sum(trend)
    if not all(isfinite(value) for value in [*scores.values(), *trend, *residuals, projected]):
        return {"status": "unavailable", "reason": "nonfinite_forecast_result"}
    paths = sorted(float(balance) + sum(trend) + sum(rng.choice(residuals) for _ in range(horizon)) for _ in range(500))
    if not all(isfinite(value) for value in paths):
        return {"status": "unavailable", "reason": "nonfinite_forecast_result"}
    return {"status": "experimental", "method": selected, "seed": seed,
            "history_days": len(daily), "validation_days": len(daily) - 14,
            "validation_mae": scores, "horizon_days": horizon,
            "projected_balance": f"{projected:.2f}",
            "p10": f"{paths[50]:.2f}", "p90": f"{paths[449]:.2f}",
            "assumption": "complete daily coverage; historical net flow; future obligations are reported separately, not deducted",
            "evaluation": "rolling origin used for method selection; not independent holdout"}


def build_audit_report(db: FinanceDB, start: date, end: date, currency: str = "RUB", account: str | None = None, category: str | None = None, now: datetime | None = None, *, forecast_horizon: int = 14) -> dict[str, Any]:
    """Build an aggregate report without exporting descriptions or credentials."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or end < start:
        raise ValueError("Valid period and timezone-aware now are required")
    all_rows = db.all_transactions()
    history = [row for row in all_rows if row["currency"] == currency and (account is None or row["account"] == account)]
    period = [row for row in history if start.isoformat() <= row["tx_date"] <= end.isoformat()]
    selected = [row for row in period if category is None or row["category"] == category]
    flow = cashflow(selected, currency)
    expected = set(db.get_setting("expected_accounts", []))
    observed = {str(row["account"]) for row in history}
    accounts = sorted((expected | observed) if account is None else {account})
    declared = db.get_setting("account_coverage", {})
    with db.connect() as conn:
        observations = [dict(row) for row in conn.execute("SELECT * FROM account_balance_observations WHERE currency = ? ORDER BY asof_date", (currency,))]
        events = [dict(row) for row in conn.execute("SELECT id,source,transaction_id,status,imported_at FROM source_events")]
        duplicate_groups = conn.execute("SELECT account, count(*) AS n FROM (SELECT account,external_id FROM transactions WHERE external_id IS NOT NULL GROUP BY account,external_id HAVING count(*) > 1) GROUP BY account").fetchall()
    duplicates = {row["account"]: row["n"] for row in duplicate_groups}
    inventory = []
    known_closing = Decimal(0)
    all_complete = bool(accounts)
    balances_known = bool(accounts)
    for i, name in enumerate(accounts):
        rows = [row for row in history if row["account"] == name]
        coverage = declared.get(name, {})
        complete = bool(coverage.get("complete") and coverage.get("start", "9999") <= start.isoformat() and coverage.get("end", "") >= end.isoformat())
        all_complete = all_complete and complete and not duplicates.get(name)
        by_date: dict[str, set[Decimal]] = {}
        for row in observations:
            if row["account"] == name:
                by_date.setdefault(row["asof_date"], set()).add(Decimal(row["amount"]))
        values = {day: next(iter(amounts)) for day, amounts in by_date.items() if len(amounts) == 1}
        opening = values.get((start - timedelta(days=1)).isoformat())
        closing = values.get(end.isoformat())
        check = reconcile(period, name, currency, start, end, opening, closing)
        balances_known = balances_known and closing is not None
        if closing is not None:
            known_closing += closing
        inventory.append({"account": f"A{i + 1}", "transaction_count": len(rows),
                          "observed_from": min((row["tx_date"] for row in rows), default=None),
                          "observed_through": max((row["tx_date"] for row in rows), default=None),
                          "coverage_complete": complete, "source": coverage.get("source"),
                          "duplicate_external_groups": duplicates.get(name, 0),
                          "reconciliation": {**check.to_dict(), "account": f"A{i + 1}"}})
    ids = set(flow["transaction_ids"])
    lineage = [event for event in events if event["transaction_id"] in ids]
    sources = sorted({str(event["source"]) for event in lineage})
    missing_lineage = sorted(ids - {int(event["transaction_id"]) for event in lineage})
    pending = sum(event["status"] == "pending_review" for event in events)
    rules = db.get_setting("rules", {"category_limits": {}})
    budgets = [{"category": row.category, "spent": str(row.spent), "limit": str(row.limit),
                "remaining": str(row.remaining), "status": row.status} for row in category_statuses(selected, rules)]
    daily = []
    if all_complete:
        for offset in range((end - start).days + 1):
            day = (start + timedelta(days=offset)).isoformat()
            daily.append(float(cashflow([row for row in period if row["tx_date"] == day], currency)["movement"]))
    expenses = [abs(Decimal(str(row["amount"]))) for row in selected if Decimal(str(row["amount"])) < 0]
    threshold = statistics.median(expenses) * 3 if len(expenses) >= 5 else None
    unusual = [int(row["id"]) for row in selected if threshold is not None and -Decimal(str(row["amount"])) > threshold]
    previous_start = start - timedelta(days=(end - start).days + 1)
    previous = cashflow([row for row in history if previous_start.isoformat() <= row["tx_date"] < start.isoformat() and (category is None or row["category"] == category)], currency)
    totals: dict[str, str | None] = {key: str(flow[key]) for key in ("income", "expenses", "net", "movement", "excluded_movement", "unresolved_movement")}
    if not selected and not all_complete:
        totals = dict.fromkeys(totals)
    subscription_groups: dict[str, list[dict[str, Any]]] = {}
    for row in history:
        if row["category"] == "subscriptions" and Decimal(row["amount"]) < 0:
            subscription_groups.setdefault(str(row["merchant"]), []).append(row)
    subscriptions = [{"transaction_ids": [row["id"] for row in group], "occurrences": len(group),
                      "average_amount": str(-sum((Decimal(row["amount"]) for row in group), Decimal(0)) / len(group)),
                      "status": "candidate; requires confirmation"} for group in subscription_groups.values() if len(group) >= 2]
    obligations = db.get_setting("obligations", None)
    safe_obligations = None if obligations is None else [{key: entry.get(key) for key in ("id", "amount", "currency", "due_date", "source")} for entry in obligations]
    verified = all_complete and not pending and not missing_lineage and all(item["reconciliation"]["status"] == "reconciled" for item in inventory)
    return {"generated_at": current.isoformat(), "filters": {"start": start.isoformat(), "end": end.isoformat(), "currency": currency, "account_filtered": account is not None, "category": category},
            "status": "complete" if verified else "incomplete",
            "totals": totals, "categories": {k: str(v) for k, v in sorted(flow["by_category"].items())},
            "accounts": inventory, "sources": sources, "source_health": source_health(db, sorted(set(sources) | {"sber_mcp"}), current),
            "lineage": {"transaction_ids": sorted(ids), "source_event_ids": [event["id"] for event in lineage], "missing_transaction_ids": missing_lineage},
            "pending_review": pending, "budgets": budgets, "unusual_transaction_ids": unusual,
            "subscriptions": subscriptions,
            "obligations": safe_obligations,
            "comparison": {"start": previous_start.isoformat(), "end": (start - timedelta(days=1)).isoformat(), "observed_net": str(previous["net"]) if previous["transaction_ids"] else None, "coverage_verified": False},
            "forecast": liquidity_forecast(daily, known_closing if balances_known and verified else None, horizon=forecast_horizon),
            "warnings": ["Observed totals are not reconciled bank balances", "Missing sources/legacy IDs/unknown obligations require review"] if not verified else []}


def render_audit_report(report: dict[str, Any]) -> str:
    """Render aggregate totals and their limitations for Telegram."""
    filters = report["filters"]
    totals = report["totals"]
    return (f"Учёт {filters['start']}…{filters['end']} ({filters['currency']})\n"
            f"Покрытие: {report['status']}; счета: {len(report['accounts'])}\n"
            f"Доход: {totals['income']}; расходы: {totals['expenses']}; net: {totals['net']}\n"
            f"Pending review: {report['pending_review']}; без lineage: {len(report['lineage']['missing_transaction_ids'])}\n"
            f"Прогноз: {report['forecast']['status']}\n"
            f"Источники: {', '.join(report['sources']) or 'нет подтверждённых источников'}\n"
            "Суммы — наблюдаемые операции; сверка остатков приведена в JSON /export-audit.")
