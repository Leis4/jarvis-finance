"""Evidence-only triage of missing operations; never authorize writes."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from jarvis.finance.bank.sber_statement import PDF_DETAIL_RE, _match_pdf_tx_line


def normalize_text(value: str) -> str:
    """Normalize case, whitespace, masked cards and explicit service wording."""
    text = " ".join(value.casefold().split())
    text = re.sub(r"[•*]{2,}\s*(\d{4})", r"card:\1", text)
    text = re.split(r"\.\s*операция по (?:карте|счету|счёту)", text, maxsplit=1)[0]
    text = re.sub(r"^перевод (?:от|для|в|из)\s+", "", text)
    return text.strip()


def extract_card_dates(text: str) -> list[dict[str, Any]]:
    """Retain operation/processing dates and auth codes from paired PDF lines."""
    evidence: list[dict[str, Any]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = " ".join(raw.split())
        start = _match_pdf_tx_line(line)
        if start:
            evidence.append({"operation_date": date.fromisoformat("-".join(reversed(start["date"].split(".")))).isoformat(),
                             "processing_date": None, "authorization_code": None, "line": number})
            continue
        detail = PDF_DETAIL_RE.match(line)
        if detail and evidence and evidence[-1]["processing_date"] is None:
            evidence[-1]["processing_date"] = "-".join(reversed(detail["date"].split(".")))
            evidence[-1]["authorization_code"] = detail["auth"]
            evidence[-1]["processing_line"] = number
    return evidence


def _words(row: dict[str, Any]) -> set[str]:
    text = normalize_text(str(row.get("merchant", "")) + " " + str(row.get("description", "")))
    stop = {"перевод", "операция", "карте", "счету", "между", "своими", "сбп", "card"}
    return {w for w in re.findall(r"[^\W\d_]+", text) if len(w) > 2 and w not in stop}


def triage_operations(targets: list[dict[str, Any]], db_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify evidence without applying account or event decisions."""
    ids = Counter((r["account"], r.get("external_id")) for r in db_rows if r.get("external_id"))
    items = []
    for target in targets:
        pdf = target["operation"]
        amount = Decimal(pdf["amount"])
        dates = {pdf["tx_date"]}
        if target.get("processing_date"):
            dates.add(target["processing_date"])
        candidates = []
        for db in db_rows:
            same_amount = amount == Decimal(db["amount"])
            opposite = amount != 0 and amount == -Decimal(db["amount"])
            text_equal = any(
                len(normalize_text(str(pdf.get(f, "")))) >= 4
                and normalize_text(str(pdf.get(f, ""))) == normalize_text(str(db.get(f, "")))
                for f in ("merchant", "description")
            )
            overlap = sorted(_words(pdf) & _words(db))
            text_support = text_equal or len(overlap) >= 2
            observed_date = db["tx_date"] in dates
            offset = min(abs((date.fromisoformat(db["tx_date"]) - date.fromisoformat(d)).days) for d in dates)
            same_id = bool(target.get("native_external_id") and target["native_external_id"] == db.get("external_id"))
            transfer_text = " ".join(str(r.get(f, "")).casefold() for r in (pdf, db) for f in ("description", "merchant"))
            transfer = opposite and observed_date and any(w in transfer_text for w in ("перевод", "списание", "пополнение", "вклад", "между своими"))
            if not (same_id or (same_amount and observed_date) or (same_amount and text_support and offset <= 3) or transfer):
                continue
            duplicate = bool(db.get("external_id") and ids[(db["account"], db["external_id"])] > 1)
            currency_equal = pdf["currency"] == db["currency"]
            db_card_markers = sorted(set(re.findall(r"[•*]{2,}\s*(\d{4})", str(db.get("merchant", "")) + " " + str(db.get("description", "")))))
            pdf_card_markers = sorted(set(re.findall(r"операция по карте\s+[•*]{2,}\s*(\d{4})", str(pdf.get("description", "")).casefold())))
            quality = ("exact/high-confidence" if same_amount and observed_date and text_equal and currency_equal and not duplicate
                       else "likely" if same_amount and text_support and currency_equal and not duplicate else "ambiguous")
            reasons = {"missing lineage"}
            if db["account"] == "sber":
                reasons.add("sber bucket")
            if db["tx_date"] != pdf["tx_date"]:
                reasons.add("date offset")
            if duplicate:
                reasons.add("duplicate external ID")
            if transfer:
                reasons.add("transfer pair")
            if not text_equal:
                reasons.add("parser/text mismatch")
            candidates.append({"db_id": db["id"], "db_operation": db, "quality": quality,
                               "reasons": sorted(reasons), "signed_amount_equal": same_amount,
                               "currency_equal": currency_equal, "observed_date_equal": observed_date,
                               "operation_date_equal": db["tx_date"] == pdf["tx_date"],
                               "processing_date_equal": db["tx_date"] == target.get("processing_date"),
                               "date_offset_days": offset, "date_offset_proven": observed_date,
                               "normalized_text_equal": text_equal, "supporting_words": overlap,
                               "account_equal": db["account"] == target["local_account"],
                               "native_external_id_equal": same_id, "transfer_opposite_sign": transfer})
            candidates[-1].update({"pdf_explicit_card_markers": pdf_card_markers, "db_text_card_markers": db_card_markers,
                                   "card_identity_verified": False,
                                   "card_note": "DB text markers may name a counterparty; confirmed document mapping is separate."})
        strong = [c for c in candidates if c["quality"] == "exact/high-confidence"]
        likely = [c for c in candidates if c["quality"] == "likely"]
        quality = ("exact/high-confidence" if len(strong) == 1 else "likely" if not strong and len(likely) == 1
                   else "ambiguous" if candidates else "no candidate")
        items.append({**target, "classification": quality, "candidates": candidates, "apply_allowed": False})
    # A strong DB event used by several targets is still ambiguous.
    usage = Counter(c["db_id"] for item in items for c in item["candidates"] if c["quality"] == "exact/high-confidence")
    for item in items:
        if item["classification"] == "exact/high-confidence" and any(
            usage[c["db_id"]] > 1 for c in item["candidates"] if c["quality"] == "exact/high-confidence"
        ):
            item["classification"] = "ambiguous"
        item["recommended_action"] = ("confirm mapping" if item["classification"] == "exact/high-confidence"
                                      else "request source" if item["classification"] == "no candidate" else "keep pending_review")
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, item in enumerate(items):
        reasons = {r for c in item["candidates"] for r in c["reasons"]} or {"missing lineage"}
        for reason in reasons:
            groups[(reason, item["classification"])].append(i)
    return {"database_modified": False, "apply_allowed": False, "items": items,
            "classification_counts": dict(Counter(i["classification"] for i in items)),
            "groups": [{"reason": reason, "classification": quality, "count": len(indices),
                        "item_indices": indices, "example_indices": indices[:3],
                        "recommended_action": items[indices[0]]["recommended_action"]}
                       for (reason, quality), indices in sorted(groups.items())],
            "group_counts_overlap": True,
            "policy": "Amounts alone never establish candidates. Opposite signs indicate transfer review only. "
                      "Near-date offsets are hypotheses unless operation/processing dates were observed. "
                      "Missing lineage applies to all legacy candidates. Confidence never authorizes import."}
