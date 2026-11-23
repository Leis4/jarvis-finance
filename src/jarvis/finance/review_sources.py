"""Verified PDF sources and honest product reports for a private workspace."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.account_timeline import account_timeline
from jarvis.finance.audit_report import build_audit_report
from jarvis.finance.db import FinanceDB
from jarvis.finance.ledger import finite_money
from jarvis.finance.production_intake import build_statement_preview
from jarvis.finance.review_staging import _guard
from jarvis.finance.review_store import ReviewStore, encoded


def attach_sources(store: ReviewStore, folder: Path) -> dict[str, Any]:
    """Freeze verified statement summaries without modifying any financial DB."""
    packet = json.loads(store.metadata()["packet"])
    mapping = {i["bank_account"]: i["local_account"] for i in store.items()}
    previews = []
    for source in packet["pdf_sources"]:
        name = source["file"]
        if Path(name).name != name:
            raise ValueError("Unsafe statement filename")
        preview = build_statement_preview(folder / name, mapping)
        if preview["source"]["sha256"] != source["sha256"] or preview["status"] != "checks_passed_read_only":
            raise ValueError("Statement identity or balance changed; regenerate review")
        previews.append({key: preview[key] for key in ("source", "metadata", "reconciliation", "parser", "issues")})
    registry = {"folder": str(folder.resolve()), "previews": previews}
    payload = encoded(registry)
    with store.connect() as conn:
        old = conn.execute("SELECT value FROM review_meta WHERE key='sources'").fetchone()
        if old and old[0] != payload:
            raise ValueError("Frozen source registry differs; preserve old workspace")
        if not old:
            conn.executemany("INSERT INTO review_meta VALUES (?,?)", [("sources", payload),
                              ("sources_hash", hashlib.sha256(payload.encode()).hexdigest())])
            conn.commit()
    return {"sources": len(previews), "production_database_modified": False}


def source_registry(store: ReviewStore) -> dict[str, Any] | None:
    store.audit()
    meta = store.metadata()
    if "sources" not in meta:
        return None
    if hashlib.sha256(meta["sources"].encode()).hexdigest() != meta["sources_hash"]:
        raise ValueError("Source registry integrity failure")
    return dict(json.loads(meta["sources"]))


def source_pdf(store: ReviewStore, sha: str) -> Path:
    """Return only a hash-verified original from the frozen source registry."""
    registry = source_registry(store)
    if registry:
        for preview in registry["previews"]:
            if preview["source"]["sha256"] == sha:
                path = Path(str(registry["folder"])) / str(preview["source"]["path_name"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                    raise ValueError("Original PDF changed")
                return path
    from jarvis.finance.statement_product import StatementProduct
    product = StatementProduct(store)
    for source in product.sources():
        if source['accepted'] and source['sha256'] == sha and source['format'] == 'pdf':
            return store.workspace / 'statement-originals' / str(source['original'])
    raise ValueError("Verified original PDF unavailable")


def workspace_report(store: ReviewStore, asof: date | None = None) -> dict[str, Any]:
    """Separate verified PDF balances from unaccepted legacy ledger reports."""
    registry = source_registry(store)
    from jarvis.finance.bank import sber_statement
    current_parser = "sber-pdf:" + hashlib.sha256(Path(sber_statement.__file__).read_bytes()).hexdigest()
    checked = asof or datetime.now(UTC).date()
    summaries = []
    accounts = []
    errors = []
    for index, saved in enumerate(registry["previews"] if registry else []):
        sha = saved["source"]["sha256"]
        try:
            if current_parser != store.metadata()["parser_version"]:
                raise ValueError("Runtime parser differs from reviewed parser")
            path = source_pdf(store, sha)
            metadata = saved["metadata"]
            preview = build_statement_preview(path, {metadata["bank_account"]: metadata["local_account"]})
            summary = {key: preview[key] for key in ("source", "metadata", "reconciliation", "parser", "issues")}
            if summary != saved or preview["status"] != "checks_passed_read_only":
                raise ValueError("Parsed statement differs from frozen evidence")
            timeline = account_timeline(preview["transactions"], account=metadata["bank_account"],
                                        currency=metadata["currency"], start=date.fromisoformat(metadata["period"][0]),
                                        end=date.fromisoformat(metadata["period"][1]), opening=metadata["opening"],
                                        closing=metadata["closing"], source_sha256=sha, complete=True, asof=checked)
            accounts.append({"alias": f"A{index + 1}", "card": metadata["card"], "account_type": metadata["account_type"],
                             "mapping_status": metadata["mapping_status"], **timeline})
            if timeline["status"] == "available":
                summaries.append(summary)
            else:
                errors.append({"source_sha256": sha, "status": timeline["status"], "reason": "Statement balance is not current/verified"})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"source_sha256": sha, "status": "unavailable", "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
            accounts.append({"alias": f"A{index + 1}", "source_sha256": sha, "status": "unavailable", "verified_closing": None})
    balances: dict[tuple[str, str], Decimal] = {}
    for p in summaries:
        key = (p["metadata"]["currency"], p["metadata"]["period"][1])
        balances[key] = balances.get(key, Decimal(0)) + Decimal(p["metadata"]["closing"])
    ledger: dict[str, Any] = {"status": "unavailable", "reason": "No managed staging rehearsal"}
    latest = store.workspace / "last-rehearsal.json"
    if latest.exists():
        batch = json.loads(latest.read_text(encoding="utf-8"))["batch_id"]
        path = _guard(store, batch)
        if summaries:
            start = min(p["metadata"]["period"][0] for p in summaries)
            end = max(p["metadata"]["period"][1] for p in summaries)
            # Existing audit service is reused on the disposable copy, never production.
            ledger = build_audit_report(FinanceDB(path), date.fromisoformat(start), date.fromisoformat(end))
    from jarvis.finance.statement_product import StatementProduct
    try:
        historical = StatementProduct(store).history(asof=checked)
    except (ValueError, KeyError, OSError):
        historical = {"status": "unavailable", "scope": "HISTORICAL_ACCEPTED_PDF_FACTS",
                      "reason": "Accepted source evidence unavailable; no stale-cache fallback"}
    return {"status": "incomplete", "production_database_modified": False,
            "historical": historical, "statement_periods_differ_scope": "fresh_current_balance_statements_only",
            "generated_document_evidence": _document_evidence(store),
            "checked_asof": checked.isoformat(), "account_register": accounts, "source_errors": errors,
            "statements": summaries, "statement_periods_differ": len({tuple(p["metadata"]["period"]) for p in summaries}) > 1,
            "assets": {"status": "partial_source_observations" if balances else "unavailable",
                       "known_bank_closing_subtotals": [{"currency": c, "asof": d, "amount": str(a)} for (c, d), a in sorted(balances.items())],
                       "total_assets": None, "note": "Four mapped bank sources do not establish all cash/investments/other assets."},
            "liabilities": {"status": "unavailable", "amount": None, "reason": "No confirmed liability inventory"},
            "budget": {"status": "unverified", "verified_spending": None, "observed_legacy_categories": ledger.get("budgets", [])},
            "transfers": {"status": "pending_review", "confirmed_pairs": None, "reason": "Signs and counterparties alone do not verify own-transfer linkage"},
            "economic_cashflow": {"status": "unverified", "income": None, "expenses": None},
            "legacy_ledger": ledger, "forecast": ledger.get("forecast", {"status": "insufficient_data"}),
            "review": {"pending": sum(i["status"] == "pending_review" for i in store.items()),
                       "identity_decisions": sum(e.get("domain") != "financial_fact" for e in store.audit())},
            "warnings": ["PDF balance checks do not reconcile the legacy ledger.",
                         "Statement movements have different periods and are not consolidated economic income.",
                         "Legacy categories/budget/transfer classifications require human review.",
                         "Eight local account labels are not evidence of eight physical bank accounts."]}


def _document_evidence(store: ReviewStore) -> dict[str, Any] | None:
    if json.loads(store.metadata()["packet"]).get("source_kind") != "generated_pdf":
        return None
    from jarvis.finance.review_pdf_evidence import generated_source_state
    return generated_source_state(store)


def accepted_capacity_inputs(
    store: ReviewStore, fact_ids: dict[str, str], *, start: date, end: date, currency: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve separate human financial facts on a consistent private snapshot."""
    from jarvis.finance.review_backup import review_snapshot

    current = now or datetime.now(UTC)
    if (current.tzinfo is None or end < start or start < current.astimezone(UTC).date() or
            len(currency) != 3 or not currency.isascii() or not currency.isalpha() or currency != currency.upper()):
        raise ValueError("Aware clock and current/future inclusive planning horizon required")
    result: dict[str, Any] = {"status": "insufficient_data", "inputs": None,
        "period": [start.isoformat(), end.isoformat()], "currency": currency,
        "database_modified": False, "scope": "human_accepted_source_facts_not_bank_authentication"}
    if set(fact_ids) != {"inventory", "balances", "obligations", "reserve"}:
        return {**result, "reason": "Four separate accepted financial facts required"}
    try:
        with review_snapshot(store) as snapshot:
            facts = {kind: snapshot.financial_fact(ident) for kind, ident in fact_ids.items()}
            for kind, fact in facts.items():
                event = fact["decision"]
                if fact["kind"] != kind or event is None or event["action"] != "accept_financial_fact":
                    return {**result, "reason": "Separate human acceptance missing or revoked"}
                stamp = datetime.fromisoformat(event["timestamp"])
                if stamp.tzinfo is None or stamp > current or current - stamp > timedelta(hours=24):
                    return {**result, "status": "stale", "reason": "Financial acceptance expired or future"}
            inventory = facts["inventory"]["original"]
            if inventory.get("complete_liquid_inventory") is not True or not inventory.get("accounts"):
                return {**result, "reason": "Full liquid inventory not explicitly accepted"}
            accounts = inventory["accounts"]
            identities = [row["account"] for row in accounts]
            if len(set(identities)) != len(identities) or any(not ident for ident in identities):
                raise ValueError("Duplicate or empty inventory identity")
            if any(row["currency"] != currency or row.get("liquid") is not True or not row.get("bank_account") for row in accounts):
                return {**result, "reason": "Currency mismatch or unknown liquid account mapping"}
            mapping = {row["account"]: row["bank_account"] for row in accounts}
            if len(set(mapping.values())) != len(mapping):
                raise ValueError("Repeated physical account mapping")
            balance_source = facts["balances"]
            if balance_source["original"].get("inventory_fact_id") != fact_ids["inventory"]:
                return {**result, "reason": "Balances bound to a different inventory"}
            rows = balance_source["original"]["balances"]
            if len(rows) != len(accounts) or {r["account"] for r in rows} != set(identities):
                return {**result, "reason": "Incomplete or duplicate balance coverage"}
            balances = []
            for row in rows:
                if row.get("amount") is None or row["currency"] != currency or row["bank_account"] != mapping[row["account"]]:
                    return {**result, "reason": "Unknown balance or mapping/currency mismatch"}
                stamp = datetime.fromisoformat(row["as_of"])
                if stamp.tzinfo is None:
                    raise ValueError("Balance time must be aware")
                # PDF dates come from the frozen parser output, never a caller's new clock.
                if row.get("statement_sha256"):
                    registry = source_registry(snapshot)
                    saved = next(p for p in (registry or {}).get("previews", []) if p["source"]["sha256"] == row["statement_sha256"])
                    path = source_pdf(snapshot, row["statement_sha256"])
                    meta = saved["metadata"]
                    preview = build_statement_preview(path, {meta["bank_account"]: meta["local_account"]})
                    if ({key: preview[key] for key in ("source", "metadata", "reconciliation", "parser", "issues")} != saved or
                            preview["status"] != "checks_passed_read_only" or row["bank_account"] != meta["bank_account"] or
                            row["account"] != meta["local_account"] or row["currency"] != meta["currency"] or
                            finite_money(row["amount"]) != finite_money(meta["closing"])):
                        raise ValueError("Statement balance/parser/mapping evidence differs")
                    closing_day = date.fromisoformat(meta["period"][1])
                    if closing_day >= current.astimezone(UTC).date():
                        return {**result, "reason": "PDF has no proven intraday closing time"}
                    stamp = datetime.combine(closing_day, datetime.min.time(), UTC)
                if stamp > current or current - stamp > timedelta(hours=24):
                    return {**result, "status": "stale", "reason": "Historical or future balance is not current cash"}
                balances.append({"account": row["account"], "currency": currency, "amount": str(finite_money(row["amount"])),
                    "as_of": stamp.isoformat(), "liquid": True, "coverage_confirmed": True,
                    "source_sha256": balance_source["source_sha256"]})
            obligation_source = facts["obligations"]["original"]
            if (obligation_source.get("complete_for_period") is not True or
                    obligation_source.get("period") != result["period"] or obligation_source.get("currency") != currency):
                return {**result, "reason": "Obligation inventory currency/horizon not accepted"}
            obligations = Decimal(0)
            seen: set[str] = set()
            occurrences: set[tuple[str, str]] = set()
            included = []
            for row in obligation_source["obligations"]:
                key = row["occurrence_id"]
                if not key or key in seen:
                    raise ValueError("Duplicate obligation occurrence; manual review required")
                seen.add(key)
                due = date.fromisoformat(row["due_date"])
                occurrence = (row["obligation_id"], due.isoformat())
                if not occurrence[0] or occurrence in occurrences:
                    raise ValueError("Repeated obligation and due date; manual review required")
                occurrences.add(occurrence)
                amount = finite_money(row["amount"])
                if amount < 0 or row["currency"] != currency or row.get("confirmed") is not True or not row.get("source_reference"):
                    return {**result, "reason": "Unconfirmed obligation or currency/amount mismatch"}
                if start <= due <= end:
                    obligations += amount
                    included.append(key)
            reserve_source = facts["reserve"]["original"]
            if reserve_source.get("currency") != currency or reserve_source.get("amount") is None or not reserve_source.get("source_reference"):
                return {**result, "reason": "Unknown reserve amount/currency/source"}
            reserve = finite_money(reserve_source["amount"])
            if reserve < 0:
                raise ValueError("Reserve must be nonnegative")
            for ident in fact_ids.values():
                snapshot.financial_fact(ident)  # originals must still match after computation
            if store.metadata() != snapshot.metadata() or store.audit() != snapshot.audit():
                return {**result, "reason": "Review evidence changed during snapshot computation"}
            provenance = {kind: {"fact_id": f["fact_id"], "source_sha256": f["source_sha256"],
                "decision_seq": f["decision"]["seq"], "accepted_at": f["decision"]["timestamp"]} for kind, f in facts.items()}
            digest = hashlib.sha256(encoded({"facts": provenance, "period": result["period"], "currency": currency}).encode()).hexdigest()
            return {**result, "status": "accepted_evidence", "inputs": {"balances": balances,
                "expected_accounts": identities, "obligations": str(obligations), "reserve": str(reserve),
                "planning_source_sha256": digest}, "provenance": provenance, "included_obligations": included,
                "inclusion_rule": "start <= due_date <= end; each occurrence_id exactly once",
                "limitations": "Human source attestation; no bank authentication, FX or future income"}
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, StopIteration, ArithmeticError) as exc:
        return {**result, "status": "unavailable", "reason": type(exc).__name__}
