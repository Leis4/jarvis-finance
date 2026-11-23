"""Private resumable read evidence; never authenticates or writes a ledger."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.finance.db import FinanceDB
from jarvis.finance.domain import Transaction
from jarvis.finance.ledger import finite_money, record_source_event
from jarvis.finance.legacy_reconciliation import compare_operations
from jarvis.mcp.client import MCPClient
from jarvis.mcp.errors import MCPError
from jarvis.mcp.policy import require_read_capability
from jarvis.mcp.stdio_client import StdioMCPClient

PARSER_VERSION = "sber-mcp-page-v1"


def digest(value: Any) -> str:
    """Hash canonical retained evidence, rejecting non-JSON numbers."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _seal(packet: dict[str, Any]) -> dict[str, Any]:
    packet.pop("evidence_sha256", None)
    packet["evidence_sha256"] = digest(packet)
    return packet


def _persist(packet: dict[str, Any], path: Path | None) -> None:
    """Atomically retain the last accepted page in an operator-owned private path."""
    if path is None:
        return
    if path.is_symlink() or path.parent.is_symlink() or path.suffix != ".json":
        raise ValueError("Unsafe checkpoint path")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".sber-checkpoint-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(_seal(packet), file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _read(client: MCPClient | StdioMCPClient, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    require_read_capability("sber", tool, args)
    result = client.call_tool(tool, args)
    if not isinstance(result, dict):
        raise TypeError("Malformed MCP response")
    if result.get("status") != "ok":
        if result.get("code") in {"session_not_found", "session_expired", "auth_required"}:
            raise PermissionError("auth_required")
        raise ValueError("Sber source unavailable")
    if not isinstance(result.get("data"), dict):
        raise TypeError("Malformed MCP data")
    return result["data"]  # type: ignore[no-any-return]


def collect_history(
    client: MCPClient | StdioMCPClient, *, session_id: str, expected_profile: str,
    start: date, end: date, checkpoint: dict[str, Any] | None = None,
    limit: int = 50, window_days: int = 31, max_pages: int = 100,
    checkpoint_path: Path | None = None,
    resource: str | None = None,
) -> dict[str, Any]:
    """Read bounded windows, retaining each page and its exact request for resume."""
    if (not expected_profile or start > end or type(window_days) is not int or
            not 1 <= window_days <= 366 or type(max_pages) is not int or
            not 1 <= max_pages <= 1000 or type(limit) is not int or not 1 <= limit <= 50):
        raise ValueError("Invalid explicit history scope")
    scope = {"profile": expected_profile, "start": start.isoformat(), "end": end.isoformat(),
             "limit": limit, "window_days": window_days, "parser_version": PARSER_VERSION}
    selected_resource = resource
    if selected_resource is not None:
        require_read_capability("sber", "sber_operations_page", {"session_id": session_id,
            "resource": selected_resource, "limit": limit, "offset": 0,
            "from_date": start.isoformat(), "to_date": end.isoformat()})
        scope["resource"] = selected_resource
    if checkpoint_path is not None:
        if checkpoint_path.is_symlink() or checkpoint_path.parent.is_symlink() or checkpoint_path.suffix != ".json":
            raise ValueError("Unsafe checkpoint path")
        if checkpoint is None and checkpoint_path.exists():
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if checkpoint is not None:
        packet = json.loads(json.dumps(checkpoint))
        old_hash = packet.pop("evidence_sha256", None)
        if old_hash != digest(packet) or packet.get("scope") != scope:
            raise ValueError("Checkpoint integrity or scope mismatch")
    else:
        packet = {"scope": scope, "pages": [], "windows": [], "products": None,
                  "products_sha256": None, "database_modified": False, "apply_allowed": False}
    packet["status"] = "partial"
    packet["source_error"] = None
    try:
        info = _read(client, "sber_session_info", {"session_id": session_id, "check_live": False})
        if info.get("profile") != expected_profile:
            raise ValueError("profile_mismatch")
        if info.get("state") != "authenticated" or info.get("session_id") != session_id:
            raise PermissionError("auth_required")
        products = _read(client, "sber_products", {"session_id": session_id})
        if any(not isinstance(products.get(key), list) for key in ("accounts", "cards")):
            raise ValueError("Malformed product inventory")
        # Balance/fetch time can legitimately change; identity must not change on resume.
        inventory = [(key, str(p.get("id")), p.get("operation_resource"), p.get("account_id"))
                     for key in ("accounts", "cards") for p in products[key] if isinstance(p, dict)]
        if len(inventory) != len(products["accounts"]) + len(products["cards"]):
            raise ValueError("Malformed product entries")
        inventory_hash = digest(inventory)
        if len(inventory) * ((end - start).days // window_days + 1) > 10_000:
            raise ValueError("History window budget exceeded")
        if packet.get("products_sha256") not in (None, inventory_hash):
            raise ValueError("Product inventory changed; new scope required")
        if packet["products"] is None:
            packet["products"] = products
            packet["products_sha256"] = inventory_hash
            resources: list[str] = []
            packet["missing_resources"] = []
            for _, ident, item_resource, _ in inventory:
                if not isinstance(item_resource, str) or not item_resource:
                    packet["missing_resources"].append(ident)
                elif item_resource not in resources:
                    resources.append(item_resource)
            for item_resource in resources:
                if selected_resource is not None and item_resource != selected_resource:
                    continue
                day = start
                while day <= end:
                    stop = min(end, day + timedelta(days=window_days - 1))
                    packet["windows"].append({"resource": item_resource, "from_date": day.isoformat(),
                                              "to_date": stop.isoformat(), "offset": 0,
                                              "coverage": "partial", "terminal": False})
                    day = stop + timedelta(days=1)
            if selected_resource is not None and selected_resource not in resources:
                raise ValueError("Selected item_resource unavailable")
        calls = 0
        for wi, window in enumerate(packet["windows"]):
            while not window["terminal"] and calls < max_pages:
                args = {"session_id": session_id, "resource": window["resource"],
                        "from_date": window["from_date"], "to_date": window["to_date"],
                        "offset": window["offset"], "limit": limit}
                data = _read(client, "sber_operations_page", args)
                calls += 1
                ops, nxt = data.get("operations"), data.get("next_offset")
                if (not isinstance(ops, list) or len(ops) > limit or
                        type(data.get("count")) is not int or data["count"] != len(ops) or
                        "next_offset" not in data or any(not isinstance(o, dict) or
                                                        not isinstance(o.get("id"), str) or not o["id"] for o in ops)):
                    raise ValueError("Malformed operation page")
                if nxt is not None and (type(nxt) is not int or nxt != window["offset"] + limit or len(ops) != limit):
                    raise ValueError("Invalid pagination cursor")
                page_hash = digest(ops)
                if ops and any(p["window"] == wi and p["operations_sha256"] == page_hash for p in packet["pages"]):
                    raise ValueError("Repeated operation page")
                packet["pages"].append({"window": wi, "request": args, "response": data,
                                         "operations_sha256": page_hash, "source_sha256": digest(data),
                                         "received_at": datetime.now(UTC).isoformat()})
                if nxt is None:
                    window["terminal"] = True
                    observed = sum(len(p["response"]["operations"]) for p in packet["pages"] if p["window"] == wi)
                    window["coverage"] = "complete" if observed else "unknown"
                else:
                    window["offset"] = nxt
                _persist(packet, checkpoint_path)
        coverage = [w["coverage"] for w in packet["windows"]]
        packet["coverage"] = ("complete" if coverage and all(c == "complete" for c in coverage)
                              and not packet["missing_resources"] else
                              "partial" if packet["pages"] else "unknown")
        packet["status"] = "read_only_evidence"
        packet["coverage_basis"] = "requested_visible_history_only_not_financial_completeness"
    except (MCPError, PermissionError, TypeError, ValueError, RuntimeError, TimeoutError) as exc:
        packet["status"] = "auth_required" if isinstance(exc, PermissionError) else "unavailable"
        packet["source_error"] = type(exc).__name__
        packet["coverage"] = "partial" if packet["pages"] else "unknown"
    _persist(packet, checkpoint_path)
    return _seal(packet)


def staging_preview(packet: dict[str, Any], workspace: Path, *,
                    confirmed_mapping: dict[str, str],
                    legacy_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Journal pending evidence in a new managed DB; reconcile supplied snapshot rows."""
    material = dict(packet)
    expected = material.pop("evidence_sha256", None)
    if expected != digest(material) or packet.get("scope", {}).get("parser_version") != PARSER_VERSION:
        raise ValueError("Unverified evidence packet")
    if workspace.is_symlink() or workspace.exists():
        raise ValueError("Staging requires a new dedicated directory")
    workspace.mkdir(parents=True, exist_ok=False)
    # The only SQLite destination is newly created inside this managed directory.
    db = FinanceDB(workspace / "candidate.sqlite")
    db.init()
    rows: dict[str, list[dict[str, Any]]] = {}
    held: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    identities: dict[str, set[str]] = {}
    with closing(sqlite3.connect(db.path)) as conn:
        conn.execute("CREATE TABLE sber_raw_events(source_sha256 TEXT, resource TEXT, native_id TEXT, original_json TEXT, UNIQUE(source_sha256,resource,native_id,original_json))")
        conn.execute("BEGIN")
        for page in packet["pages"]:
            resource = page["request"]["resource"]
            for op in page["response"]["operations"]:
                raw = json.dumps(op, sort_keys=True, ensure_ascii=False, allow_nan=False)
                conn.execute("INSERT OR IGNORE INTO sber_raw_events VALUES (?,?,?,?)",
                             (page["source_sha256"], resource, op["id"], raw))
                key = (resource, op["id"], digest(op))
                if key in seen:
                    continue
                seen.add(key)
                identities.setdefault(op["id"], set()).add(resource)
                try:
                    when = datetime.fromisoformat(op["date"])
                    if when.tzinfo is None or op.get("is_financial") is not True:
                        raise ValueError("Unproven financial date/state")
                    amount = finite_money(op["amount"]["amount"])
                    currency = op["amount"]["currency"]
                    if not isinstance(currency, str) or not currency or resource not in confirmed_mapping:
                        raise ValueError("Unconfirmed account or currency")
                    if not packet["scope"]["start"] <= when.date().isoformat() <= packet["scope"]["end"]:
                        raise ValueError("Operation outside requested scope")
                    tx = Transaction(when.date(), amount, currency, str(op.get("description") or ""),
                                     str(op.get("merchant") or ""), "pending_review",
                                     confirmed_mapping[resource], op["id"])
                    record_source_event(conn, tx, None, "sber_mcp", PARSER_VERSION, "pending_review")
                    rows.setdefault(resource, []).append({"tx_date": tx.tx_date.isoformat(),
                        "amount": str(amount), "currency": currency, "description": tx.description,
                        "merchant": tx.merchant, "category": "pending_review", "account": resource,
                        "external_id": op["id"]})
                except (KeyError, TypeError, ValueError):
                    held.append({"resource": resource, "native_id": op["id"], "reason": "pending_source_review"})
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0]
        # Replaying the journal never creates accepted transactions or extra events.
        before = count
        for group in rows.values():
            for row in group:
                tx = Transaction(date.fromisoformat(row["tx_date"]), finite_money(row["amount"]), row["currency"],
                                 row["description"], row["merchant"], "pending_review",
                                 confirmed_mapping[row["account"]], row["external_id"])
                record_source_event(conn, tx, None, "sber_mcp", PARSER_VERSION, "pending_review")
        conn.commit()
        if conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0] != before:
            raise ValueError("Staging replay added evidence")
        if conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] != 0:
            raise ValueError("Staging unexpectedly imported financial transactions")
    reconciliations = [compare_operations(group, legacy_rows, bank_account=resource,
                          local_account=confirmed_mapping[resource], start=packet["scope"]["start"],
                          end=packet["scope"]["end"]) for resource, group in rows.items()]
    overlaps = [{"native_id": ident, "resources": sorted(resources), "action": "pending_review"}
                for ident, resources in identities.items() if len(resources) > 1]
    report = {"status": "pending_review", "source_sha256": expected, "parser_version": PARSER_VERSION,
              "legacy_rows_sha256": digest(legacy_rows),
              "coverage": packet.get("coverage", "unknown"), "coverage_basis": packet.get("coverage_basis"),
              "reconciliation": reconciliations, "held": held, "cross_resource_overlaps": overlaps,
              "pending_source_events": count, "replay_verified": True,
              "balance_observations": packet.get("products"),
              "financial_report": {"status": "insufficient_data", "income": None, "expenses": None,
                  "free_investment_cash": None, "reason": "categories/sign semantics/overlap and full coverage require review"},
              "database_modified": False, "staging_database_modified": True, "apply_allowed": False}
    (workspace / "source.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    (workspace / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def native_review_packet(packet: dict[str, Any], *, confirmed_mapping: dict[str, str],
                         legacy_rows: list[dict[str, Any]] | None = None, include_unmapped: bool = False) -> dict[str, Any]:
    """Adapt immutable native pages into the existing pending human queue."""
    from jarvis.finance.review_store import encoded
    material = dict(packet)
    expected = material.pop("evidence_sha256", None)
    if expected != digest(material):
        raise ValueError("Unverified capture")
    sources: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    held = 0
    ordinal = 0
    for page in packet["pages"]:
        original = {"request": page["request"], "response": page["response"], "received_at": page["received_at"]}
        sha = hashlib.sha256(encoded(original).encode()).hexdigest()
        sources.append({"sha256": sha, "original": original})
        resource = page["request"]["resource"]
        for op in page["response"]["operations"]:
            ordinal += 1
            try:
                when = datetime.fromisoformat(op["date"])
                amount = finite_money(op["amount"]["amount"])
                currency = op["amount"]["currency"]
                if (when.tzinfo is None or not isinstance(currency, str) or not currency.strip()
                        or (not confirmed_mapping.get(resource) and not include_unmapped) or op.get("is_financial") is not True
                        or not packet["scope"]["start"] <= when.date().isoformat() <= packet["scope"]["end"]):
                    raise ValueError("Unproven observation")
                operation = {"tx_date": when.date().isoformat(), "amount": str(amount), "currency": currency,
                             "account": resource, "description": str(op.get("description") or ""),
                             "merchant": str(op.get("merchant") or ""), "category": "pending_review", "external_id": op["id"]}
                items.append({"source_kind": "sber_mcp", "source_sha256": sha, "source_ordinal": ordinal,
                              "operation": operation, "original_record": op, "bank_account": resource,
                              "local_account": confirmed_mapping.get(resource), "classification": "ambiguous",
                              "candidates": [], "reason": ("native source semantic review" if confirmed_mapping.get(resource)
                                  else "native source semantic review; local account mapping unknown"), "apply_allowed": False})
            except (KeyError, ValueError, TypeError):
                held += 1
    reconciliations: list[dict[str, Any]] = []
    if legacy_rows is not None:
        if any(not isinstance(r.get("id"), int) or isinstance(r["id"], bool) for r in legacy_rows):
            raise ValueError("Isolated legacy rows require integer record identities")
        if len({r["id"] for r in legacy_rows}) != len(legacy_rows):
            raise ValueError("Duplicate legacy record identities")
        for resource in sorted({i["bank_account"] for i in items}):
            group = [i for i in items if i["bank_account"] == resource]
            result = compare_operations([i["operation"] for i in group], legacy_rows,
                bank_account=resource, local_account=confirmed_mapping[resource],
                start=packet["scope"]["start"], end=packet["scope"]["end"])
            reconciliations.append(result)
            found = {f["pdf_index"]: f["db_index"] for f in result["found"]}
            repeated_db = {n for g in result["db_repeated_ids"] for n in g["row_indices"]}
            for index, item in enumerate(group):
                candidates = []
                for pair in result["candidate_pairs"]:
                    if pair["pdf_index"] != index:
                        continue
                    reasons = ["native source semantic review"]
                    if pair["db_index"] in repeated_db:
                        reasons.append("duplicate external ID")
                    quality = "exact/high-confidence" if found.get(index) == pair["db_index"] else "ambiguous"
                    candidates.append({**pair, "db_operation": result["db_operations"][pair["db_index"]],
                                       "quality": quality, "reasons": reasons, "apply_allowed": False})
                item.update(candidates=candidates, classification=("exact/high-confidence" if index in found
                            else "ambiguous" if candidates else "no candidate"))
    # A row or native ID observed through several resources remains unresolved.
    usage = Counter(c["db_id"] for i in items for c in i["candidates"])
    native_ids = Counter(i["operation"]["external_id"] for i in items)
    for item in items:
        overlap = native_ids[item["operation"]["external_id"]] > 1
        shared = any(usage[c["db_id"]] > 1 for c in item["candidates"])
        if overlap or shared:
            item["classification"] = "ambiguous"
            for candidate in item["candidates"]:
                candidate["quality"] = "ambiguous"
                candidate["reasons"].append("cross-resource or repeated observation")
        item["recommended_action"] = "request source" if item["classification"] == "no candidate" else "keep pending_review"
    counts = Counter(i["classification"] for i in items)
    return {"source_kind": "sber_mcp", "parser_version": PARSER_VERSION, "capture_sha256": expected,
            "native_sources": sources, "items": items, "held_unparsed": held,
            "legacy_rows_sha256": digest(legacy_rows) if legacy_rows is not None else None,
            "reconciliation": reconciliations, "classification_counts": dict(counts),
            "coverage": packet.get("coverage", "unknown"),
            "capture_scope": packet["scope"], "coverage_windows": packet["windows"],
            "capture_status": packet.get("status"), "source_error": packet.get("source_error"),
            "observed_products": packet.get("products"),
            "groups": [{"reason": "native source semantic review", "classification": quality, "count": count}
                       for quality, count in sorted(counts.items())],
            "apply_allowed": False, "database_modified": False}


def native_source(store: Any, sha: str) -> dict[str, Any]:
    """Return a verified native response without exposing session request metadata."""
    from jarvis.finance.review_store import encoded
    store.audit()
    packet = json.loads(store.metadata()["packet"])
    for source in packet.get("native_sources", []):
        if source["sha256"] == sha:
            if hashlib.sha256(encoded(source["original"]).encode()).hexdigest() != sha:
                raise ValueError("Native source changed")
            return {"response": source["original"]["response"], "source_sha256": sha, "apply_allowed": False}
    raise ValueError("Native source unavailable")


def merge_native_captures(captures: list[dict[str, Any]]) -> dict[str, Any]:
    """Refuse malformed capture metadata without exposing original payloads."""
    try:
        return _merge_native_captures(captures)
    except (KeyError, TypeError, AttributeError, IndexError, ArithmeticError) as exc:
        raise ValueError("Malformed native capture") from exc


def _merge_native_captures(captures: list[dict[str, Any]]) -> dict[str, Any]:
    """Retain immutable captures and deduplicate only identical scoped observations."""
    from jarvis.finance.review_store import _item_key

    if not captures or len(captures) > 10000:
        raise ValueError("Bounded native capture list required")
    entries: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, Any]] = {}
    windows: dict[str, dict[str, Any]] = {}
    observations: dict[str, dict[str, Any]] = {}
    identities: dict[tuple[str, str], list[str]] = {}
    namespace: tuple[str, bool] | None = None
    for capture in captures:
        if (capture.get("source_kind") != "sber_mcp" or capture.get("parser_version") != PARSER_VERSION
                or capture.get("apply_allowed") is not False or capture.get("database_modified") is not False
                or "capture_packets" in capture):
            raise ValueError("Verified native capture contract required")
        profile = capture.get("capture_scope", {}).get("profile")
        if not isinstance(profile, str) or not profile:
            raise ValueError("Native profile identity missing")
        current = (profile, capture.get("synthetic_demonstration") is True)
        if namespace is not None and namespace != current:
            raise ValueError("Native capture profile/mode mismatch")
        namespace = current
        sha = digest(capture)
        entries.setdefault(sha, {"sha256": sha, "packet": capture})
        for window in capture.get("coverage_windows", []):
            windows.setdefault(digest(window), window)
        local_sources = {}
        for source in capture["native_sources"]:
            if digest(source["original"]) != source["sha256"]:
                raise ValueError("Native original hash mismatch")
            local_sources[source["sha256"]] = source
            sources.setdefault(source["sha256"], source)
        for item in capture["items"]:
            if any(key in item for key in ("native_review_key", "observation_key", "capture_references", "conflict_reasons")):
                raise ValueError("Derived native identity cannot be supplied by capture")
            original = item["original_record"]
            source = local_sources.get(item["source_sha256"])
            op = item["operation"]
            resource, native_id = item["bank_account"], original.get("id")
            if (not source or not isinstance(native_id, str) or not native_id
                    or item.get("source_kind") != "sber_mcp" or original.get("is_financial") is not True
                    or datetime.fromisoformat(original["date"]).tzinfo is None
                    or source["original"]["request"]["resource"] != resource
                    or original not in source["original"]["response"]["operations"]
                    or item.get("apply_allowed") is not False
                    or op["external_id"] != native_id or op["account"] != resource
                    or op["currency"] != original["amount"]["currency"]
                    or finite_money(op["amount"]) != finite_money(original["amount"]["amount"])
                    or op["tx_date"] != datetime.fromisoformat(original["date"]).date().isoformat()
                    or op["description"] != str(original.get("description") or "")
                    or op["merchant"] != str(original.get("merchant") or "")):
                raise ValueError("Native observation/source binding mismatch")
            key = digest([profile, resource, native_id, original])
            ref = {"capture_sha256": sha, "source_sha256": item["source_sha256"],
                   "source_ordinal": item["source_ordinal"]}
            if key not in observations:
                observations[key] = {**item, "native_review_key": _item_key(item),
                    "observation_key": key, "capture_references": [], "conflict_reasons": []}
                identities.setdefault((profile, native_id), []).append(key)
            if ref not in observations[key]["capture_references"]:
                observations[key]["capture_references"].append(ref)
    for keys in identities.values():
        group = [observations[key] for key in keys]
        for item in group:
            reasons = []
            if len({i["bank_account"] for i in group}) > 1:
                reasons.append("cross-resource overlap")
            if sum(i["bank_account"] == item["bank_account"] for i in group) > 1:
                reasons.append("changed native payload")
            if reasons:
                item.update(conflict_reasons=reasons, classification="ambiguous",
                            recommended_action="keep pending_review")
    items = list(observations.values())
    if len({_item_key(i) for i in items}) != len(items):
        raise ValueError("Native review identity collision")
    counts = dict(Counter(i["classification"] for i in items))
    return {**captures[-1], "native_sources": list(sources.values()), "items": items,
        "coverage_windows": list(windows.values()),
        "capture_packets": list(entries.values()), "classification_counts": counts,
        "groups": [{"reason": "native source semantic review", "classification": quality, "count": count}
                   for quality, count in sorted(counts.items())],
        "capture_count": len(entries), "observation_count": len(items),
        "conflicting_observations": sum(bool(i["conflict_reasons"]) for i in items)}
