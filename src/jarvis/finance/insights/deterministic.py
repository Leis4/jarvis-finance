"""Local accepted-snapshot insights; never money apply, prediction or cloud advice."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jarvis.finance.audit_report import financial_question
from jarvis.finance.event_taxonomy import NON_ECONOMIC_CATEGORIES, canonical_category
from jarvis.finance.review_store import encoded

ENGINE_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

class InsightPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: Literal["financial-insights-v1"] = "financial-insights-v1"
    minimum_change: Decimal = Field(default=Decimal(100), ge=0, le=1_000_000_000)
    relative_change_percent: Decimal = Field(default=Decimal(20), ge=0, le=10000)
    minimum_observations: int = Field(default=3, ge=2, le=100, strict=True)
    unusual_ratio: Decimal = Field(default=Decimal(3), ge=2, le=100)
    minimum_baseline_observations: int = Field(default=5, ge=3, le=100, strict=True)
    recurring_amount_tolerance_percent: Decimal = Field(default=Decimal(0), ge=0, le=5)
    recurring_gap_variation_days: int = Field(default=5, ge=0, le=7, strict=True)
    maximum_history_records: int = Field(default=500, ge=10, le=1000, strict=True)
    maximum_cards: int = Field(default=12, ge=1, le=20, strict=True)


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def recurring_pattern(group: list[dict[str, Any]], policy: InsightPolicy) -> dict[str, Any] | None:
    """Extract the existing exact recurrence heuristic for local CFO reuse."""
    if len(group) < policy.minimum_observations:
        return None
    ordered = sorted(group, key=lambda r: (r["date"], r["transaction_id"]))
    dates = [date.fromisoformat(r["date"]) for r in ordered]
    gaps = [(b - a).days for a, b in pairwise(dates)]
    typical = median([Decimal(r["net_expense"]) for r in ordered])
    if (typical <= 0 or not gaps or min(gaps) < 6 or max(gaps) > 45
            or max(gaps) - min(gaps) > policy.recurring_gap_variation_days
            or any(abs(Decimal(r["net_expense"]) - typical) / typical * 100 > policy.recurring_amount_tolerance_percent for r in group)):
        return None
    return {"observations": len(group), "typical_net_expense": str(typical),
        "monthly_equivalent": str((typical * Decimal('30.44') / (Decimal(min(gaps) + max(gaps)) / 2)).quantize(Decimal('.01'))),
        "gap_min_days": min(gaps), "gap_max_days": max(gaps),
        "next_expected_date": (dates[-1] + timedelta(days=round(median(gaps)))).isoformat(),
        "transaction_ids": [r["transaction_id"] for r in ordered][:20], "references_truncated": len(ordered) > 20}


def quality_notice(code: str) -> dict[str, Any]:
    return {"kind": "data_quality", "headline": "Финансовые данные требуют проверки",
        "explanation": "Пока недостаточно подтверждённых финансовых данных для анализа. Проверьте принятие snapshot, свежесть, остатки, идентичности и покрытие обоих периодов.",
        "reason_code": code, "financial_conclusion": False, "action": "inspect_source"}


def financial_insights(path: Path | None, *, month: str, currency: str, now: datetime,
                       accepted_sources: frozenset[str], accepted_sha256: str | None,
                       policy: InsightPolicy) -> dict[str, Any]:
    """Derive bounded cards only after existing financial acceptance gates pass."""
    base: dict[str, Any] = {"status": "evidence_only", "analysis_status": "insufficient_data", "cards": [],
        "policy": policy.model_dump(mode="json"), "policy_sha256": digest(policy.model_dump(mode="json")),
        "engine_revision": ENGINE_REVISION,
        "financial_apply_allowed": False, "database_modified": False, "paid_calls": 0}
    if path is None:
        return {**base, "notices": [quality_notice("no_accepted_ledger")]}
    def ask(intent: str, period: str = month) -> dict[str, Any]:
        return financial_question(path, month=period, intent=intent, currency=currency,
            now=now, accepted_sources=accepted_sources, accepted_sha256=accepted_sha256)
    growth = ask("category_growth")
    if growth["status"] != "available":
        missing = ask("missing_data")
        duplicates = ask("duplicates")
        codes = [str(growth["status"])]
        info = missing.get("answer") or {}
        if info.get("incomplete_accounts"):
            codes.append("partial_account_coverage")
        if info.get("unknown_balances"):
            codes.append("unknown_balances")
        if info.get("pending_source_events"):
            codes.append("pending_source_events")
        if (duplicates.get("answer") or {}).get("observed_duplicate_groups"):
            codes.append("conflicting_operation_identities")
        if not growth.get("snapshot_accepted"):
            codes.append("unaccepted_or_invalidated_snapshot")
        return {**base, "analysis_status": growth["status"], "notices": [quality_notice(c) for c in codes]}
    current = ask("expense_history")
    previous_month = growth["comparison_period"][0][:7]
    previous = ask("expense_history", previous_month)
    if any(result["status"] != "available" or result["source_sha256"] != accepted_sha256 for result in (current, previous)):
        return {**base, "notices": [quality_notice("comparison_history_unavailable")]}
    if hashlib.sha256(path.read_bytes()).hexdigest() != accepted_sha256:
        return {**base, "analysis_status": "unavailable", "notices": [quality_notice("snapshot_changed_during_analysis")]}
    rows, prior = current["answer"]["operations"], previous["answer"]["operations"]
    if len(rows) + len(prior) > policy.maximum_history_records:
        return {**base, "notices": [quality_notice("bounded_history_limit")]}
    refs = sorted(set(current["source_refs"]) | set(previous["source_refs"]))
    binding = {key: growth[key] for key in ("source_identity", "source_sha256", "snapshot_version", "audit_revision", "audit_sha256", "period", "comparison_period", "currency", "coverage", "source_availability")}
    binding.update(source_refs=refs[:100], source_ref_count=len(refs), source_refs_truncated=len(refs) > 100)
    base.update(status="available", analysis_status="available", binding=binding, notices=[])

    def add(kind: str, subject: Any, headline: str, explanation: str, numbers: dict[str, Any], *, low: bool = False) -> None:
        identity = digest([binding, base["policy_sha256"], ENGINE_REVISION, kind, subject])
        base["cards"].append({"id": identity, "kind": kind, "headline": headline, "explanation": explanation,
            "numbers": numbers, "evidence": "low_observation_count" if low else "accepted_history",
            "confirmation_required": True, "causality_verified": False, "financial_conclusion": True,
            "action": "inspect_transaction" if kind == "unusual_expense" else "review_category"})

    def ordinary(row: dict[str, Any]) -> bool:
        allocations = row.get("allocations") or []
        categories = {canonical_category(str(p["category"])) for p in allocations} or {canonical_category(row["category"])}
        return (len(categories) == 1 and not categories & NON_ECONOMIC_CATEGORIES
            and not any("transfer" in c or c.startswith("refund") for c in categories)
            and Decimal(row["net_expense"]) > 0 and Decimal(row["signed_amount"]) < 0)

    with localcontext() as context:
        context.prec = 40
        comparison = growth["period_comparison"]
        for item in comparison["categories"]:
            now_amount, old_amount = Decimal(item["current"]), Decimal(item["previous"])
            daily, old_daily = now_amount / comparison["current_days"], old_amount / comparison["previous_days"]
            relative = (daily - old_daily) / old_daily * 100 if old_daily > 0 else None
            normalized_change = (daily - old_daily) * comparison["current_days"]
            if normalized_change == 0 or abs(normalized_change) < policy.minimum_change or (relative is not None and abs(relative) < policy.relative_change_percent):
                continue
            count = sum(r["category"] == item["category"] and ordinary(r) for r in rows)
            prior_count = sum(r["category"] == item["category"] and ordinary(r) for r in prior)
            add("spending_change", item["category"], "Изменился темп расходов: " + item["category"],
                "Сравнение чистых расходов за принятые месяцы с поправкой на число дней. Возвраты учтены; изменение не доказывает причину.",
                {**item, "relative_daily_change_percent": str(relative.quantize(Decimal("0.01"))) if relative is not None else None,
                 "current_observations": count, "previous_observations": prior_count, "current_days": comparison["current_days"], "previous_days": comparison["previous_days"]},
                low=min(count, prior_count) < policy.minimum_observations)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in prior + rows:
            if ordinary(row) and row.get("merchant_identity"):
                groups.setdefault(row["merchant_identity"], []).append(row)
        for merchant, group in sorted(groups.items()):
            pattern = recurring_pattern(group, policy)
            if pattern is None:
                continue
            add("recurring_candidate", merchant, "Возможный регулярный платёж",
                "Повторения у одного merchant с близкими суммами и интервалами. Это кандидат, а не доказанная подписка или обязательство.",
                {"merchant_identity": merchant, **pattern})
        for row in rows:
            peers = [Decimal(r["net_expense"]) for r in prior if ordinary(r) and r["category"] == row["category"]]
            if not ordinary(row) or len(peers) < policy.minimum_baseline_observations:
                continue
            baseline, amount = median(peers), Decimal(row["net_expense"])
            if amount >= baseline * policy.unusual_ratio and amount - baseline >= policy.minimum_change:
                add("unusual_expense", row["transaction_id"], "Расход заметно выше прежних операций",
                    "Чистый расход превышает медиану принятых расходов этой категории. Это статистический сигнал для проверки, не вывод о мошенничестве.",
                    {"transaction_id": row["transaction_id"], "amount": str(amount), "baseline_median": str(baseline), "baseline_observations": len(peers), "ratio": str(amount / baseline)})
    total = len(base["cards"])
    base.update(cards=base["cards"][:policy.maximum_cards], eligible_card_count=total, cards_truncated=total > policy.maximum_cards)
    return base
