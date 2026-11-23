"""Offline incremental observation rehearsal; no live activation or financial acceptance."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.finance.ledger import finite_money
from jarvis.finance.review_store import ReviewStore, encoded
from jarvis.finance.sber_intake import (
    PARSER_VERSION,
    collect_history,
    digest,
    native_review_packet,
)
from jarvis.mcp.policy import require_read_capability

KEY = "sber_sync_head"


class OfflineBankSync:
    """Coordinate bounded captures using the existing private review audit and evidence."""

    def __init__(self, store: ReviewStore, *, profile: str, start: date,
                 mapping: dict[str, str], resource: str | None = None, overlap_days: int = 7):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile) or type(overlap_days) is not int or not 1 <= overlap_days <= 31:
            raise ValueError("Explicit bounded offline sync scope required")
        self.store, self.profile, self.start = store, profile, start
        self.mapping, self.resource, self.overlap = dict(mapping), resource, overlap_days
        self.binding = digest([profile, start.isoformat(), mapping, resource, overlap_days, "offline-v1"])
        if not store.path.exists():
            store.initialize({"source_kind": "sber_mcp", "parser_version": PARSER_VERSION,
                "capture_scope": {"profile": profile}, "synthetic_demonstration": True,
                "native_sources": [], "items": [], "groups": [], "coverage_windows": [],
                "coverage": "unknown", "empty_sync_seed": True,
                "apply_allowed": False, "database_modified": False})

    def _head(self, conn: sqlite3.Connection) -> dict[str, Any]:
        events = self.store._audit(conn)
        row = conn.execute("SELECT value FROM review_meta WHERE key=?", (KEY,)).fetchone()
        evidence = [e for e in events if e.get("domain") == "sber_sync"]
        if row is None:
            if evidence:
                raise ValueError("Sync state missing")
            packet = json.loads(conn.execute("SELECT value FROM review_meta WHERE key='packet'").fetchone()[0])
            if packet.get("source_kind") != "sber_mcp" or packet.get("synthetic_demonstration") is not True:
                raise PermissionError("Offline native workspace required")
            return {"binding": self.binding, "phase": "BLOCKED", "runs": {}, "last_success": None,
                    "last_attempt": None, "covered_end": None, "snapshot": None, "active": None}
        value = json.loads(row[0])
        if not evidence or evidence[-1].get("state_sha256") != digest(value) or value.get("binding") != self.binding:
            raise ValueError("Sync state integrity/scope refused")
        return dict(value)

    def _save(self, conn: sqlite3.Connection, head: dict[str, Any], now: datetime) -> None:
        events = self.store._audit(conn)
        conn.execute("INSERT OR REPLACE INTO review_meta VALUES (?,?)", (KEY, encoded(head)))
        self.store._append_event(conn, {"seq": len(events) + 1, "domain": "sber_sync",
            "item_id": KEY, "action": "offline_sync_state", "state_sha256": digest(head),
            "timestamp": now.isoformat()})
        conn.commit()

    @staticmethod
    def _clock(now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("Aware sync clock required")

    def status(self, *, now: datetime) -> dict[str, Any]:
        self._clock(now)
        with self.store.connect() as conn:
            head = self._head(conn)
        state = head["phase"]
        if state == "SYNCING":
            state = "BLOCKED"  # Interrupted/concurrent run: never automatically repeat HTTP.
        if state == "FRESH" and now - datetime.fromisoformat(head["last_success"]) > timedelta(hours=24):
            state = "STALE"
        return {"status": state, "last_success": head["last_success"], "last_attempt": head["last_attempt"],
                "snapshot": head["snapshot"], "covered_end": head["covered_end"],
                "source_error": head.get("source_error"), "coverage": head.get("coverage", "unknown"),
                "coverage_basis": "requested_visible_history_only_not_financial_completeness",
                "financial_report_allowed": False, "apply_allowed": False, "database_modified": False,
                "live_allowed": False}

    def run(self, client: Any, *, run_id: str, session_id: str, end: date, now: datetime,
            max_pages: int = 100, limit: int = 50) -> dict[str, Any]:
        self._clock(now)
        if type(limit) is not int or not 1 <= limit <= 50 or type(max_pages) is not int or not 1 <= max_pages <= 1000:
            raise ValueError("Invalid bounded page budget")
        require_read_capability("sber", "sber_session_info", {"session_id": session_id, "check_live": False})
        if (getattr(client, "synthetic", None) is not True or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id)
                or end < self.start or end > now.date()):
            raise PermissionError("Offline synthetic transport and explicit scope required")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            head = self._head(conn)
            if run_id in head["runs"]:
                if head["runs"][run_id]["end"] != end.isoformat():
                    raise ValueError("Run identity reused with different scope")
                return {**head["runs"][run_id], "cached": True, "database_modified": False}
            if head["phase"] == "SYNCING" or len(head["runs"]) >= 1000:
                raise PermissionError("Interrupted/concurrent sync or receipt limit; explicit recovery required")
            if head["last_attempt"] and now < datetime.fromisoformat(head["last_attempt"]):
                raise ValueError("Sync clock moved backwards")
            if head["covered_end"] and end < date.fromisoformat(head["covered_end"]):
                raise ValueError("Sync window cannot move backwards")
            begin = max(self.start, date.fromisoformat(head["covered_end"]) - timedelta(days=self.overlap - 1)) if head["covered_end"] else self.start
            head.update(phase="SYNCING", last_attempt=now.isoformat(),
                        active={"id": run_id, "end": end.isoformat(), "begin": begin.isoformat()})
            self._save(conn, head, now)
        # Existing collector enforces fixed read allowlist, profile/session binding and pagination.
        packet = collect_history(client, session_id=session_id, expected_profile=self.profile,
            start=begin, end=end, resource=self.resource, max_pages=max_pages, limit=limit)
        capture = native_review_packet(packet, confirmed_mapping=self.mapping, include_unmapped=True)
        capture.update(synthetic_demonstration=True, sync_run_id=run_id, sync_binding=self.binding)
        self.store.append_native_capture(capture)
        return self._finish(capture, now=now)

    def _finish(self, capture: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            head = self._head(conn)
            active = head["active"]
            if not active or capture.get("sync_run_id") != active["id"] or capture.get("sync_binding") != self.binding:
                raise ValueError("Sync recovery binding refused")
            merged = json.loads(conn.execute("SELECT value FROM review_meta WHERE key='packet'").fetchone()[0])
            complete = (capture["coverage"] == "complete" and capture["held_unparsed"] == 0
                        and not merged.get("conflicting_observations"))
            phase = "REAUTH_REQUIRED" if capture["capture_status"] == "auth_required" else "FRESH" if complete else "INCOMPLETE"
            balances = []
            products = capture.get("observed_products") or {}
            for account in products.get("accounts", []):
                try:
                    money = account["balance"]
                    if not isinstance(money["currency"], str) or not re.fullmatch("[A-Z]{3}", money["currency"]):
                        raise ValueError
                    balances.append({"resource": account["operation_resource"], "amount": str(finite_money(money["amount"])),
                        "currency": money["currency"], "observed_at": products.get("fetched_at"),
                        "freshness": "UNVERIFIED_SDK_PORTFOLIO_CACHE"})
                except (KeyError, ValueError, TypeError):
                    phase = "INCOMPLETE" if phase == "FRESH" else phase
            version = digest([sorted(i["observation_key"] for i in merged["items"]),
                [{k: b[k] for k in ("resource", "amount", "currency")} for b in balances]])
            changed = phase == "FRESH" and (not head["snapshot"] or head["snapshot"].get("data_version") != version)
            result = {"data_changed": changed, "data_version": version if phase == "FRESH" else None, "status": phase, "end": active["end"], "start": active["begin"],
                "capture_sha256": digest(capture), "observation_count": len(merged["items"]),
                "held_unparsed": capture["held_unparsed"], "conflicting_observations": merged.get("conflicting_observations", 0),
                "coverage": capture["coverage"], "financial_report_allowed": False, "apply_allowed": False,
                "database_modified": False, "cached": False}
            if phase == "FRESH":
                head.update(covered_end=active["end"], last_success=now.isoformat(), snapshot={
                    "capture_sha256": digest(capture), "data_version": version, "observed_at": now.isoformat(),
                    "products_sha256": digest(products), "balances": balances,
                    "scope": "offline_observations_only", "financial_report_allowed": False})
            head["runs"][active["id"]] = result
            head.update(phase=phase, active=None, source_error=capture.get("source_error"), coverage=capture["coverage"])
            self._save(conn, head, now)
        return result

    def recover(self, *, now: datetime) -> dict[str, Any]:
        """Finish an already appended capture after crash, without any transport call."""
        self._clock(now)
        with self.store.connect() as conn:
            head = self._head(conn)
            packet = json.loads(conn.execute("SELECT value FROM review_meta WHERE key='packet'").fetchone()[0])
        if head["phase"] != "SYNCING":
            return self.status(now=now)
        retained = [e["packet"] for e in packet.get("capture_packets", [])]
        capture = next((p for p in retained if p.get("sync_run_id") == head["active"]["id"]
                        and p.get("sync_binding") == self.binding), None)
        if capture is None:
            return self.status(now=now)  # Unknown transport outcome: retain claim, no resend.
        return self._finish(capture, now=now)


def owner_preflight(candidate: Path | None = None) -> dict[str, Any]:
    """Inspect only immutable candidate code; never open a banking profile or start a worker."""
    from jarvis.finance.sber_runtime_candidate import verify_candidate

    identity = None
    if candidate is not None:
        try:
            identity = verify_candidate(candidate)
        except Exception:  # noqa: BLE001 - fixed diagnostic, no paths/secrets
            return {"status": "BLOCKED", "reason": "candidate_integrity_unverified", "live_allowed": False}
    return {"status": "BLOCKED", "candidate_identity": identity, "live_allowed": False,
        "requirements": ["native_Tk_cause_unknown", "owner_security_runtime_acceptance",
                         "owner_one_session_one_resource_read_consent", "multipage_sync_not_authorized",
                         "balance_cache_freshness_unverified", "financial_semantics_unaccepted"],
        "bank_requests": 0, "profile_read": False, "database_modified": False}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Sber offline-sync owner preflight; never authenticates")
    parser.add_argument("--preflight", action="store_true", required=True)
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args()
    print(encoded(owner_preflight(args.candidate)))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
