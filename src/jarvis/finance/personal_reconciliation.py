"""Explain bank coverage and normalize account/card observations without double counting."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from jarvis.finance.ledger import finite_money, reconcile


def identity(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def owned_endpoint(value: Any, owned: dict[tuple[str, str], str]) -> str | None:
    if not isinstance(value, dict):
        return None
    return owned.get((str(value.get("type")), str(value.get("id"))))


def normalize_capture(capture: dict[str, Any], ledger: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve only exact native identity or explicit card/account bindings."""
    products, history = capture["products"], capture["history"]
    accounts = {str(a["id"]): a for a in products["accounts"]}
    if len(accounts) != len(products["accounts"]):
        raise ValueError("Duplicate bank inventory")
    resources: dict[str, str] = {}
    owned: dict[tuple[str, str], str] = {}
    card_resources: set[str] = set()
    for ident, item in accounts.items():
        if item.get("operation_resource"):
            resources[item["operation_resource"]] = ident
        owned[("ct-account" if item.get("kind") == "ctaccount" else "account", ident)] = ident
    for card in products.get("cards", []):
        account = str(card.get("account_id", ""))
        resource = card.get("operation_resource") or "card:" + str(card["id"])
        if account in accounts:
            resources[resource] = account
            owned[("card", str(card["id"]))] = account
            card_resources.add(resource)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    decisions: list[dict[str, Any]] = []
    for page in history.get("pages", []):
        resource = page["request"]["resource"]
        for op in page["response"]["operations"]:
            ref = {"resource": resource, "native_id": op.get("id"), "source_sha256": identity(op)}
            reason = None
            if resource not in resources:
                reason = "unknown_account"
            elif op.get("is_financial") is not True:
                reason = "nonfinancial_excluded"
            elif op.get("is_hidden") is True:
                reason = "hidden_financial_operation"
            if reason:
                decisions.append({**ref, "reason": reason})
                continue
            try:
                stamp = datetime.fromisoformat(op["date"])
                amount = finite_money(op["amount"]["amount"])
                currency = op["amount"]["currency"]
                if (stamp.tzinfo is None or not op.get("id") or
                        not history["scope"]["start"] <= stamp.date().isoformat() <= history["scope"]["end"]):
                    raise ValueError("Invalid operation time")
                if currency != accounts[resources[resource]]["balance"]["currency"]:
                    raise ValueError("Billing currency requires review")
                if amount != amount.quantize(Decimal("0.01")):
                    raise ValueError("Invalid money precision")
                record = {"account": resources[resource], "native_id": str(op["id"]), "date": stamp.date().isoformat(),
                    "stamp": stamp.isoformat(), "amount": str(amount), "currency": currency,
                    "merchant": str(op.get("merchant") or ""), "description": str(op.get("description") or ""),
                    "resource": resource, "source_sha256": ref["source_sha256"], "raw": op}
                grouped[(record["account"], record["native_id"])].append(record)
            except (ValueError, KeyError, TypeError, ArithmeticError):
                decisions.append({**ref, "reason": "incomplete_or_ambiguous_money"})
    normalized = []
    for (account, native_id), group in sorted(grouped.items()):
        financial = {(r["stamp"], r["amount"], r["currency"], identity(r["raw"].get("billing_amount"))) for r in group}
        primary = next((r for r in group if r["resource"] not in card_resources), group[0])
        ref = {"account": account, "native_id": native_id, "source_sha256": primary["source_sha256"]}
        if len(financial) != 1:
            decisions.append({**ref, "reason": "conflicting_native_identity", "observations": len(group)})
            continue
        exact = [r for r in ledger if r["account"] == account and r.get("external_id") == native_id]
        if any((str(r["tx_date"]), finite_money(r["amount"]), r["currency"]) !=
               (primary["date"], finite_money(primary["amount"]), primary["currency"]) for r in exact):
            decisions.append({**ref, "reason": "ledger_money_conflict", "bank_amount": primary["amount"],
                "ledger_amounts": [r["amount"] for r in exact], "currency": primary["currency"], "bank_date": primary["date"]})
            continue
        if not exact:
            similar = [r for r in ledger if r["account"] == account and r["tx_date"] == primary["date"]
                       and r["currency"] == primary["currency"] and finite_money(r["amount"]) == finite_money(primary["amount"])]
            if similar:
                decisions.append({**ref, "reason": "ambiguous_different_id"})
                continue
        raw = primary["raw"]
        origin = owned_endpoint(raw.get("from_resource"), owned)
        destination = owned_endpoint(raw.get("to_resource"), owned)
        internal = bool(origin and destination and origin != destination and (
            account == origin and finite_money(primary["amount"]) < 0 or
            account == destination and finite_money(primary["amount"]) > 0))
        normalized.append({**primary, "internal_transfer_proven": internal})
        decisions.append({**ref, "reason": "accepted_card_only" if all(r["resource"] in card_resources for r in group) else "accepted_native",
                          "representations": len(group), "internal_transfer_proven": internal})
        seen_resources = {primary["resource"]}
        for duplicate in group:
            if duplicate is not primary:
                decisions.append({"account": account, "native_id": native_id, "source_sha256": duplicate["source_sha256"],
                    "reason": "repeated_observation" if duplicate["resource"] in seen_resources else "duplicate_representation"})
                seen_resources.add(duplicate["resource"])
    counts = dict(Counter(d["reason"] for d in decisions))
    held = sum(v for k, v in counts.items() if k not in {
        "accepted_native", "accepted_card_only", "duplicate_representation", "repeated_observation", "nonfinancial_excluded"})
    windows = []
    for w in history.get("windows", []):
        pages = [p for p in history["pages"] if p["request"]["resource"] == w["resource"]
                 and p["request"]["from_date"] == w["from_date"] and p["request"]["to_date"] == w["to_date"]]
        count = sum(len(p["response"]["operations"]) for p in pages)
        windows.append({"resource": w["resource"], "period": [w["from_date"], w["to_date"]],
            "terminal": w["terminal"], "observations": count,
            "status": "terminal_empty_unverified" if w["terminal"] and count == 0 else "terminal_observed" if w["terminal"] else "incomplete"})
    return {"records": normalized, "decisions": decisions, "counts": counts, "held": held,
        "windows": windows, "protocol_complete": bool(windows) and all(w["terminal"] for w in windows) and not history.get("missing_resources"),
        "financial_history_complete": False, "receipt_sha256": identity(decisions)}


def data_status(db: Any, capture: dict[str, Any], snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Report per-account journal movement, gaps and comparable snapshot arithmetic."""
    rows = db.all_transactions()
    report = normalize_capture(capture, rows)
    current = snapshots[-1] if snapshots else db.get_setting("cfo.bank_snapshot", {})
    previous = next((s for s in reversed(snapshots[:-1])
        if datetime.fromisoformat(s["observed_at"]) < datetime.fromisoformat(current["observed_at"])), None)
    per_account = []
    for account in current.get("accounts", []):
        ident, currency = account["account"], account["currency"]
        selected = [r for r in rows if r["account"] == ident and r["currency"] == currency]
        period = current["period"]
        movement = reconcile(selected, ident, currency, datetime.fromisoformat(period[0]).date(),
            datetime.fromisoformat(period[1]).date(), None, finite_money(account["amount"]))
        interval: dict[str, Any] = {"status": "needs_comparable_snapshots", "discrepancy": None}
        old = next((a for a in previous["accounts"] if a["account"] == ident and a["currency"] == currency), None) if previous else None
        if old and previous:
            lo, hi = datetime.fromisoformat(previous["observed_at"]), datetime.fromisoformat(current["observed_at"])
            changes = [r for r in report["records"] if r["account"] == ident and lo < datetime.fromisoformat(r["stamp"]) <= hi]
            delta = sum((finite_money(r["amount"]) for r in changes), Decimal(0))
            difference = finite_money(account["amount"]) - finite_money(old["amount"]) - delta
            interval = {"status": "arithmetic_consistent_coverage_unproven" if difference == 0 else "possible_discrepancy",
                "period": [lo.isoformat(), hi.isoformat()], "opening": old["amount"], "movement": str(delta),
                "closing": account["amount"], "discrepancy": str(difference), "full_reconciliation": False}
        per_account.append({"account": ident, "currency": currency, "bank_balance": account["amount"],
            "ledger_movement_all_time": str(sum((finite_money(r["amount"]) for r in selected), Decimal(0))),
            "ledger_balance": None, "opening_balance": None, "known_period_movement": str(movement.movement),
            "transactions": len(selected), "held_observations": sum(d.get("account") == ident for d in report["decisions"] if d["reason"] in {
                "ledger_money_conflict", "ambiguous_different_id", "conflicting_native_identity"}),
            "own_transfer_movements": str(sum((finite_money(r["amount"]) for r in report["records"]
                if r["account"] == ident and r["internal_transfer_proven"]), Decimal(0))),
            "interval_check": interval, "reason": "Opening balance and complete financial coverage not established"})
    inventory = {"accounts": [{"id": a["id"], "kind": a.get("kind"), "currency": a["balance"]["currency"],
        "amount": a["balance"]["amount"], "state": a.get("state"), "restricted": a.get("arrested") is True}
        for a in capture["products"]["accounts"]],
        "cards": [{"id": c["id"], "account_id": c.get("account_id"), "balance_added_to_total": False}
                  for c in capture["products"].get("cards", [])]}
    return {"status": "partial", "observed_at": current.get("observed_at"), "inventory": inventory,
        "accounts": per_account, "resources": len(capture["products"]["accounts"]) + len(capture["products"].get("cards", [])),
        "windows": report["windows"], "counts": report["counts"], "held": report["held"],
        "protocol_complete": report["protocol_complete"], "financial_history_complete": False,
        "receipt_sha256": report["receipt_sha256"], "review": [d for d in report["decisions"] if d["reason"] not in {
            "accepted_native", "accepted_card_only", "duplicate_representation", "repeated_observation", "nonfinancial_excluded"}],
        "next_steps": ["Подтвердить начальный остаток/полноту периода для полной сверки; данные не подгоняются.",
                       "Пустые терминальные окна наблюдены, отсутствие операций за всю историю не доказано."]}
