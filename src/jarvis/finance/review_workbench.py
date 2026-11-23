"""Read-only operator overview; observations never create accepted financial facts."""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from jarvis.finance.review_backup import review_snapshot
from jarvis.finance.review_store import ReviewStore

KINDS = ("inventory", "balances", "obligations", "reserve")


def bank_coverage(packet: dict[str, Any], *, now: datetime,
                  review_items: list[dict[str, Any]] | None = None, audit_revision: int = 0) -> dict[str, Any]:
    """Describe saved observed coverage, never certify financial completeness."""
    from jarvis.finance.readiness import saved_source_entry
    from jarvis.finance.sber_intake import digest

    products = packet.get("observed_products") or {}
    fetched = products.get("fetched_at")
    freshness = saved_source_entry({"status": "ok", "last_success_at": fetched, "checked_at": fetched}, now)
    windows = packet.get("coverage_windows", [])
    sources = packet.get("native_sources", [])
    items = packet["items"] if review_items is None else review_items
    retained: dict[tuple[str, str], dict[str, Any]] = {}
    for capture in [entry["packet"] for entry in packet.get("capture_packets", [])] + [packet]:
        for kind in ("accounts", "cards"):
            for product in (capture.get("observed_products") or {}).get(kind, []):
                retained[(kind, product["id"])] = product
    held_counts = [entry["packet"].get("held_unparsed") for entry in packet.get("capture_packets", [])] or [packet.get("held_unparsed")]
    held = sum(held_counts) if all(type(value) is int and value >= 0 for value in held_counts) else None
    resources = []
    for kind in ("accounts", "cards"):
        for product in [p for (k, _), p in retained.items() if k == kind]:
            resource = product.get("operation_resource")
            pages = [s["original"] for s in sources if resource and s["original"]["request"].get("resource") == resource]
            hashes = [s["sha256"] for s in sources if resource and s["original"]["request"].get("resource") == resource]
            records = [op for page in pages for op in page["response"]["operations"]]
            observations = [i for i in items if resource and i["bank_account"] == resource]
            dates = []
            for record in records:
                try:
                    stamp = datetime.fromisoformat(record["date"])
                    if stamp.tzinfo is not None:
                        dates.append(stamp.date().isoformat())
                except (KeyError, TypeError, ValueError):
                    pass  # held records cannot establish an observed date
            payloads: dict[str, set[str]] = {}
            for record in records:
                payloads.setdefault(record["id"], set()).add(digest(record))
            unique_payloads = sum(len(values) for values in payloads.values())
            periods = [{"start": w["from_date"], "end": w["to_date"],
                "visible_history": w["coverage"], "terminal": w["terminal"],
                "next_offset": None if w["terminal"] else w["offset"]}
                for w in windows if resource and w["resource"] == resource]
            # Old packets lack window checkpoints: do not infer terminal/completeness.
            if not periods:
                periods = [{"start": p["request"].get("from_date"), "end": p["request"].get("to_date"),
                    "visible_history": "unknown", "terminal": None, "next_offset": None} for p in pages]
            resources.append({"kind": kind, "resource": resource, "product_id": product.get("id"),
                "last4": product.get("last4"), "status": "observed" if pages else "not_read" if resource else "unavailable",
                "pages_read": len(pages), "observations": sum(i["bank_account"] == resource for i in packet["items"]),
                "observed_records": sum(len(p["response"]["operations"]) for p in pages),
                "periods": periods, "financial_complete": None,
                "inventory_presence": "latest_observation" if any(p.get("id") == product["id"] for p in products.get(kind, [])) else "historical_observation",
                "source_hashes": hashes, "observed_period": {"start": min(dates), "end": max(dates)} if dates else None,
                "repeated_observations": len(records) - unique_payloads,
                "conflicting_native_ids": sum(len(values) > 1 for values in payloads.values()),
                "conflicting_observations": sum(bool(i.get("conflict_reasons")) for i in observations),
                "review_states": dict(Counter(i.get("status", "pending_review") for i in observations)),
                "financial_accepted_records": None})
    return {"status": "unavailable" if packet.get("source_error") else "partial",
        "resources": resources, "period": packet.get("capture_scope"), "balance_freshness": freshness,
        "source_error": "Source read failed (redacted)" if packet.get("source_error") else None,
        "financial_complete": None, "basis": "requested_visible_history_only_not_financial_completeness",
        "source_revision": digest(packet), "parser_version": packet.get("parser_version"),
        "audit_revision": audit_revision, "held_unparsed": held,
        "coverage_percentage": None, "database_modified": False, "apply_allowed": False}


def workbench_overview(store: ReviewStore, *, now: datetime | None = None) -> dict[str, Any]:
    """Show current trust and source availability without filling money gaps."""
    from jarvis.finance.review_availability import source_availability
    from jarvis.finance.review_sources import workspace_report
    with review_snapshot(store) as frozen:
        meta, items, audit = frozen.metadata(), frozen.items(), frozen.audit()
        packet = json.loads(meta["packet"])
        facts: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KINDS}
        for key in meta:
            if not key.startswith("financial_fact:"):
                continue
            ident = key.split(":", 1)[1]
            saved = json.loads(meta[key])
            if saved["kind"] not in facts:
                continue
            try:
                fact = frozen.financial_fact(ident)
                decision = fact["decision"]
                state = "human_reviewed_source_claim" if decision and decision["action"] == "accept_financial_fact" else "pending_review"
                if decision and decision["action"] == "revoke_financial_fact":
                    state = "revoked"
                original = fact["original"]
                facts[fact["kind"]].append({"fact_id": ident, "status": state,
                    "decision_revision": decision["seq"] if decision else 0,
                    "source_sha256": fact["source_sha256"], "currency": original.get("currency"),
                    "synthetic_demonstration": original.get("synthetic_demonstration") is True,
                    "period": original.get("period"), "bank_authenticity_verified": False})
            except (OSError, ValueError, KeyError, TypeError):
                facts[saved["kind"]].append({"fact_id": ident, "status": "unavailable", "bank_authenticity_verified": False})
        ids: dict[str, str] = {}
        missing, conflicting = [], []
        for kind, rows in facts.items():
            accepted = [r for r in rows if r["status"] == "human_reviewed_source_claim"]
            if len(accepted) == 1:
                ids[kind] = accepted[0]["fact_id"]
            elif not accepted:
                missing.append(kind)
            else:
                conflicting.append(kind)
        result = {"status": "read_only_review", "trust": "source_claims_not_bank_authentication",
            "source_kind": packet.get("source_kind", "pdf"),
            "synthetic_demonstration": packet.get("synthetic_demonstration") is True,
            "review": {"total": len(items), "statuses": dict(Counter(i["status"] for i in items)),
                "classifications": dict(Counter(i["classification"] for i in items)),
                "revision": max((e["seq"] for e in audit), default=0),
                "conflicts": sum(i["classification"] == "ambiguous" or len(i.get("candidates", [])) > 1 for i in items)},
            "facts": facts, "capacity_input_selection": {"status": "requires_scope_validation" if not missing and not conflicting else "insufficient_data",
                "fact_ids": ids, "missing": missing, "conflicting": conflicting},
            "unknown": {"total_assets": None, "liabilities": None, "goals_progress": None,
                        "income": None, "expenses": None, "free_cash": None},
            "formula": "capacity preview = accepted liquid balances - reserve - due obligations - explicit scenario spending",
            "limitations": ["Observations and identity decisions do not accept financial meaning",
                "Unique accepted facts still require currency/period/freshness/completeness validation",
                "Scenario spending is an assumption; future income and FX are not inferred"],
            "apply_allowed": False, "database_modified": False}
    result["sources"] = source_availability(store)
    products = packet.get("observed_products")
    result["observed_banking"] = None
    if packet.get("source_kind") == "sber_mcp" and isinstance(products, dict):
        coverage = bank_coverage(packet, now=now or datetime.now(UTC), review_items=items,
            audit_revision=max((e["seq"] for e in audit), default=0))
        from jarvis.mcp.adapters.sber import rub_account_subtotal
        try:
            subtotal: str | None = str(rub_account_subtotal(products))
        except (TypeError, ValueError):
            subtotal = None
        availability = coverage["balance_freshness"]["status"]
        if availability != "available":
            subtotal = None
        result["observed_banking"] = {"status": "observed_only" if subtotal is not None else
            availability if availability != "available" else "insufficient_data",
            "balance_availability": availability, "history": coverage,
            "rub_account_subtotal": subtotal, "coverage": packet.get("coverage", "unknown"),
            "synthetic": packet.get("synthetic_demonstration") is True, "financial_source_approved": False,
            "accounts": products.get("accounts", []), "cards": products.get("cards", [])}
    result["financial_report"] = workspace_report(store)
    if store.metadata() != meta or store.audit() != audit:
        return {"status": "unavailable", "reason": "review_changed", "apply_allowed": False, "database_modified": False}
    return result
