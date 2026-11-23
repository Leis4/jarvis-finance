"""Validate a private semantic worksheet without granting financial authority."""
from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any, NoReturn

FIELDS = (
    "amount_signed_direction", "billing_amount_currency", "currency", "commission",
    "state_cancelled_pending", "is_financial_is_hidden", "balance_after",
    "card_account_overlap", "refunds_internal_transfers", "history_retention",
    "balance_snapshot_freshness", "transaction_settlement_dates", "account_ownership",
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def review_summary(worksheet: dict[str, Any], *, evidence_hashes: set[str]) -> dict[str, Any]:
    """Return only sanitized completeness evidence, never an import/report permit."""
    def reject() -> NoReturn:
        raise ValueError("Invalid semantic worksheet")

    if (worksheet.get("format") != "jarvis-sber-semantic-review-v1"
            or any(worksheet.get(k) is not False for k in
                   ("apply_allowed", "database_modified", "financial_report_allowed"))
            or worksheet.get("status") != "blocked"
            or not all(isinstance(h, str) and _HASH.fullmatch(h) for h in evidence_hashes)):
        reject()
    rows = worksheet.get("observations")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        reject()
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for row in rows:
        field, status, hashes, rule = (row.get(k) for k in ("field", "status", "evidence_hashes", "rule"))
        if (not isinstance(field, str) or field not in FIELDS or field in seen
                or not isinstance(status, str) or status not in {"unknown", "confirmed", "conflicting", "unavailable"}
                or not isinstance(hashes, list)
                or any(not isinstance(h, str) or h not in evidence_hashes for h in hashes)
                or len(hashes) != len(set(hashes))
                or (rule is not None and (not isinstance(rule, str) or not rule.strip()))):
            reject()
        if status == "confirmed" and (not hashes or rule is None):
            reject()
        seen.add(field)
        counts[status] += 1
    missing = sorted(set(FIELDS) - seen)
    identity_complete = True
    for key in ("source_capture_sha256", "original_sqlite_sha256", "server_code_sha256", "sdk_code_sha256"):
        value = worksheet.get(key)
        if value is None:
            identity_complete = False
        elif (not isinstance(value, str) or not _HASH.fullmatch(value)
              or (key == "source_capture_sha256" and value not in evidence_hashes)):
            reject()
    for key in ("server_version", "sdk_version", "reviewed_by"):
        value = worksheet.get(key)
        if value is None:
            identity_complete = False
        elif not isinstance(value, str) or not value.strip():
            reject()
    timestamp = worksheet.get("reviewed_at_utc")
    scope = worksheet.get("scope")
    if not isinstance(scope, dict):
        reject()
    try:
        if timestamp is None:
            identity_complete = False
        elif datetime.fromisoformat(timestamp).utcoffset() != UTC.utcoffset(None):
            reject()
        if scope.get("start") is None or scope.get("end") is None or scope.get("resource_alias") is None:
            identity_complete = False
        elif (date.fromisoformat(scope["start"]) > date.fromisoformat(scope["end"])
              or not isinstance(scope["resource_alias"], str) or not scope["resource_alias"].strip()):
            reject()
    except (ValueError, TypeError):
        reject()
    complete = identity_complete and not missing and counts["confirmed"] == len(FIELDS)
    return {"status": "blocked", "worksheet_complete": complete,
            "identity_complete": identity_complete, "missing_fields": missing,
            "observation_counts": dict(sorted(counts.items())),
            "coverage": "unverified", "bank_authenticity_verified": False,
            "security_acceptance_verified": False, "apply_allowed": False,
            "financial_report_allowed": False, "database_modified": False}
