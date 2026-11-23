"""Bind private review decisions and original evidence to a specific rehearsal."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jarvis.finance.review_store import ReviewStore, _item_key, encoded, source_ordinal


def decision_binding(store: ReviewStore) -> dict[str, Any]:
    """Hash audited financial decisions separately from human category labels."""
    meta = store.metadata()
    packet = json.loads(meta["packet"])
    return {"packet_hash": meta["packet_hash"], "parser_version": meta["parser_version"],
            "snapshot_sha256": packet.get("snapshot_sha256"),
            "source_hashes": sorted(s["sha256"] for s in packet.get("native_sources", packet.get("pdf_sources", []))),
            "decision_hash": hashlib.sha256(encoded(store.audit()).encode()).hexdigest()}


def provenance_matrix(store: ReviewStore) -> dict[str, Any]:
    """Expose identities, not public bank numbers or transaction descriptions."""
    from jarvis.finance.bank import sber_statement
    from jarvis.finance.review_backup import review_snapshot
    from jarvis.finance.review_staging import _guard
    with review_snapshot(store) as frozen:
        binding = decision_binding(frozen)
        items = frozen.items()
        packet = json.loads(frozen.metadata()["packet"])
        diagnostic_groups = []
        for group in packet.get('groups', []):
            indices = group.get('item_indices', [])
            if any(type(i) is not int or not 0 <= i < len(packet['items']) for i in indices):
                raise ValueError('Invalid diagnostic packet index')
            diagnostic_groups.append({**group, 'item_ids': [_item_key(packet['items'][i]) for i in indices],
                                      'item_indices_basis': 'immutable_packet_items_not_matrix_position'})
        from jarvis.finance.statement_product import StatementProduct
        accepted_sources = [s for s in StatementProduct(store).sources(frozen) if s['accepted']]
        pdf_acceptance = {'status': 'accepted' if accepted_sources else 'unavailable',
                          'sources': len(accepted_sources), 'observations': sum(len(s['transactions']) for s in accepted_sources),
                          'source_acceptance_confirms_categories': False, 'native_apply_allowed': False}
    if packet.get("source_kind") in {"sber_mcp", "generated_pdf"}:
        from jarvis.finance.sber_intake import PARSER_VERSION
        generated = packet.get("source_kind") == "generated_pdf"
        parser_version = PARSER_VERSION
        if generated:
            from jarvis.finance.review_pdf_evidence import parser_identity
            parser_version = parser_identity()
        return {"status": "blocked", "reason": "Generated document financial source unapproved" if generated else "Native financial apply not accepted",
                "binding": binding, "runtime_parser": parser_version, "rehearsal_binding": None,
                "matrix": [{"item_id": i["id"], "source_sha256": i["source_sha256"],
                            "source_ordinal": source_ordinal(i), "decision_revision": i["revision"],
                            "decision_action": i["status"], "apply_allowed": False} for i in items],
                "groups": packet.get("groups", []), "database_modified": False,
                "apply_allowed": False, "financial_accepted": False}
    parser = "sber-pdf:" + hashlib.sha256(Path(sber_statement.__file__).read_bytes()).hexdigest()
    status, reason = "insufficient_data", "No provenance-bound rehearsal"
    rehearsal: dict[str, Any] | None = None
    latest = store.workspace / "last-rehearsal.json"
    if latest.exists():
        try:
            batch = json.loads(latest.read_text(encoding="utf-8"))["batch_id"]
            path = _guard(store, batch)
            manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
            rehearsal = manifest.get("provenance")
            if rehearsal is None:
                status, reason = "stale", "Historical rehearsal has no decision/parser binding"
            elif rehearsal != binding:
                status, reason = "stale", "Audited decision/source/parser identity changed"
            elif hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get("candidate_sha256"):
                status, reason = "stale", "Candidate changed since replay validation (including rollback)"
            else:
                status, reason = "available", "Current decision/source/parser binding matches rehearsal"
                if any(hashlib.sha256((path.parent / name).read_bytes()).hexdigest() != manifest["backup_sha256"]
                       for name in ["before.sqlite", "restore-verification.sqlite"]):
                    status, reason = "unavailable", "Backup/restore identity mismatch"
                if hashlib.sha256(Path(manifest["source_snapshot_path"]).read_bytes()).hexdigest() != binding["snapshot_sha256"]:
                    status, reason = "stale", "Source SQLite changed after rehearsal"
                for source in packet.get("pdf_sources", []):
                    if Path(source["file"]).name != source["file"]:
                        raise ValueError("Unsafe original source name")
                    original = Path(manifest["source_directory"]) / source["file"]
                    if hashlib.sha256(original.read_bytes()).hexdigest() != source["sha256"]:
                        status, reason = "stale", "Original source changed after rehearsal"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            status, reason = "unavailable", type(exc).__name__
    if parser != binding["parser_version"]:
        status, reason = "stale", "Runtime parser differs from reviewed parser"
    rows = [{"item_id": i["id"], "source_sha256": i["source_sha256"], "source_ordinal": i["pdf_index"] + 1,
             "original_record_hash": hashlib.sha256(encoded(i["operation"]).encode()).hexdigest(),
             "decision_revision": i["revision"], "decision_action": i["status"],
             "parser_version": binding["parser_version"], "apply_allowed": False,
             "staging_proposal": i["status"] == "approve_new"} for i in items]
    return {"status": status, "reason": reason, "binding": binding, "rehearsal_binding": rehearsal,
            "scope": "LEGACY_LEDGER_REHEARSAL", "pdf_source_acceptance": pdf_acceptance,
            "historical_analysis_requires_legacy_rehearsal": False,
            "runtime_parser": parser, "matrix": rows, "groups": diagnostic_groups,
            "diagnostic_group_memberships": sum(len(g['item_ids']) for g in diagnostic_groups),
            "diagnostic_group_unique_items": len({i for g in diagnostic_groups for i in g['item_ids']}),
            "diagnostic_groups_are_decisions": False,
            "database_modified": False, "apply_allowed": False, "financial_accepted": False}
