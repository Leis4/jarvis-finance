"""Conservative operation matching without database writes or identity decisions."""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from typing import Any


def _text(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = re.split(r"\.\s*операция по", text, maxsplit=1)[0]
    text = re.sub(r"(?:\s+(?:moscow|москва))?\s+rus$", "", text)
    return " ".join(re.findall(r"\w+", text))


def _core(row: dict[str, Any]) -> tuple[str, Decimal, str]:
    return str(row["tx_date"]), Decimal(str(row["amount"])), str(row["currency"]).upper()


def _text_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return any(
        _text(left.get(field)) and _text(left.get(field)) == _text(right.get(field))
        for field in ("merchant", "description")
    )


def _groups(rows: list[dict[str, Any]], identity: bool) -> list[dict[str, Any]]:
    groups: dict[Any, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        key = row.get("external_id") if identity else (
            *_core(row), row["account"], _text(row.get("description")), _text(row.get("merchant")),
        )
        if key:
            groups[key].append(index)
    return [{"row_indices": indices, "external_id": rows[indices[0]].get("external_id"),
             "differing_fields": [field for field in ("tx_date", "amount", "currency", "account", "description", "merchant")
                                  if len({str(rows[i].get(field)) for i in indices}) > 1]}
            for indices in groups.values() if len(indices) > 1]


def compare_operations(
    pdf_rows: list[dict[str, Any]], db_rows: list[dict[str, Any]], *,
    bank_account: str, local_account: str, start: str, end: str,
) -> dict[str, Any]:
    """Match only unique financial/text evidence; retain ambiguous candidates."""
    if not bank_account or not local_account or end < start:
        raise ValueError("Confirmed mapping and valid period required")
    if any(row["account"] != bank_account for row in pdf_rows):
        raise ValueError("PDF bank identity differs from confirmed mapping")
    selected = [row for row in db_rows if row["account"] == local_account and start <= row["tx_date"] <= end]
    outside = [row for row in db_rows if row["account"] == local_account and not start <= row["tx_date"] <= end]
    pdf = [dict(row, account=local_account) for row in pdf_rows]
    conflicts = _groups(selected, True)
    pdf_ids = _groups(pdf, True)
    blocked_db = {i for group in conflicts for i in group["row_indices"]}
    blocked_pdf = {i for group in pdf_ids for i in group["row_indices"]}
    pairs: list[dict[str, Any]] = []
    strict_pdf: dict[int, list[int]] = defaultdict(list)
    strict_db: dict[int, list[int]] = defaultdict(list)
    candidate_pdf: set[int] = set()
    candidate_db: set[int] = set()
    for pi, left in enumerate(pdf):
        for di, right in enumerate(selected):
            same_id = bool(left.get("external_id") and left.get("external_id") == right.get("external_id"))
            same_core = _core(left) == _core(right)
            same_text = _text_matches(left, right)
            # A shared amount alone never forms a candidate. Near evidence remains unresolved.
            candidate = same_core or same_id or (same_text and (
                left["tx_date"] == right["tx_date"] or Decimal(left["amount"]) == Decimal(right["amount"])))
            if not candidate:
                continue
            candidate_pdf.add(pi)
            candidate_db.add(di)
            strict = same_core and same_text
            if strict:
                strict_pdf[pi].append(di)
                strict_db[di].append(pi)
            differences = [field for field in ("tx_date", "currency", "description", "merchant", "external_id")
                           if str(left.get(field)) != str(right.get(field))]
            if Decimal(left["amount"]) != Decimal(right["amount"]):
                differences.append("signed_amount")
            pairs.append({"pdf_index": pi, "db_index": di, "db_id": right.get("id"),
                          "differences": differences, "financial_fields_equal": same_core,
                          "normalized_text_equal": same_text, "external_id_equal": same_id,
                          "bank_account": bank_account, "local_account": local_account,
                          "account_equal_after_confirmed_mapping": True})
    found = [{"pdf_index": pi, "db_index": ds[0], "db_id": selected[ds[0]].get("id")}
             for pi, ds in strict_pdf.items() if len(ds) == 1 and len(strict_db[ds[0]]) == 1
             and pi not in blocked_pdf and ds[0] not in blocked_db]
    found_pdf = {x["pdf_index"] for x in found}
    found_db = {x["db_index"] for x in found}
    missing = [i for i in range(len(pdf)) if i not in candidate_pdf]
    absent = [i for i in range(len(selected)) if i not in candidate_db]
    unresolved_pdf = sorted(candidate_pdf - found_pdf)
    unresolved_db = sorted(candidate_db - found_db)
    return {
        "status": "BLOCKED" if missing or absent or unresolved_pdf or unresolved_db else "matched_read_only",
        "database_modified": False, "bank_account": bank_account, "local_account": local_account,
        "period": [start, end], "pdf_operations": pdf_rows, "db_operations": selected,
        "db_outside_period": outside, "found": found, "missing_pdf_indices": missing,
        "absent_db_indices": absent, "unresolved_pdf_indices": unresolved_pdf,
        "unresolved_db_indices": unresolved_db, "candidate_pairs": pairs,
        "pdf_duplicates": _groups(pdf, False), "db_duplicates": _groups(selected, False),
        "pdf_repeated_ids": pdf_ids, "db_repeated_ids": conflicts,
        "conflicting_external_ids": [g for g in conflicts if g["differing_fields"]],
        "coverage": {"pdf_to_db": len(found) / len(pdf) if pdf else None,
                     "db_to_pdf": len(found) / len(selected) if selected else None,
                     "pdf_denominator": len(pdf), "db_denominator": len(selected)},
        "counts": {"found": len(found), "pdf_missing_no_candidate": len(missing),
                   "db_absent_no_candidate": len(absent), "pdf_unresolved": len(unresolved_pdf),
                   "db_unresolved": len(unresolved_db)},
        "policy": "Unique date/signed amount/currency/confirmed account and equal normalized merchant or description. "
                  "Normalization removes punctuation, PDF operation suffix and Moscow/RUS suffix. "
                  "Repeated IDs and ambiguous matches stay unresolved. Missing means no candidate under this policy, "
                  "not permission to import. Parser-derived PDF IDs cannot establish native bank identity.",
    }
