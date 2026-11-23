"""Read-only Sber statement metadata extraction and reconciliation previews."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.bank.sber_statement import _extract_pdf_text, read_sber_export
from jarvis.finance.ledger import transaction_payload

_MONEY = r"[-+]?\d[\d\s\u00a0]*[,.]\d{2}"
_PERIOD_RE = re.compile(r"За период\s+(\d{2}\.\d{2}\.\d{4})\s+[—-]\s+(\d{2}\.\d{2}\.\d{4})")
_ACCOUNT_RE = re.compile(r"Номер сч[её]та\s+((?:\d\s*){20})", re.IGNORECASE)
_CARD_RE = re.compile(r"Карты, привязанные к сч[её]ту.*?(••\s*\d{4}|\*{2}\s*\d{4})", re.IGNORECASE)


def _money(value: str) -> Decimal:
    return Decimal(value.replace("\u00a0", " ").replace(" ", "").replace(",", ".").replace("+", ""))


def _amount_after(text: str, label: str, occurrence: int = 0) -> Decimal | None:
    matches = list(re.finditer(re.escape(label) + r"\s+(" + _MONEY + r")", text, re.IGNORECASE))
    if occurrence >= len(matches):
        return None
    return _money(matches[occurrence].group(1))


def _summary_amount(text: str, label: str, next_label: str) -> Decimal | None:
    """Read a summary amount whose explanatory text spans several PDF lines."""
    section = text.split(label, 1)[1].split(next_label, 1)[0] if label in text and next_label in text else ""
    match = re.search(r"(" + _MONEY + r")", section)
    return _money(match.group(1)) if match else None


def _date(value: str) -> str:
    return date.fromisoformat("-".join(reversed(value.split(".")))).isoformat()


def extract_statement_metadata(path: Path, local_mapping: dict[str, str] | None = None) -> dict[str, Any]:
    """Extract declared scope and account identity from an original PDF."""
    text = _extract_pdf_text(path)
    account_match = _ACCOUNT_RE.search(text)
    account = re.sub(r"\s+", "", account_match.group(1)) if account_match else None
    period_match = _PERIOD_RE.search(text)
    type_match = re.search(r"Тип сч[её]та\s+(.+)", text, re.IGNORECASE)
    statement_type = type_match.group(1).strip() if type_match else (
        "payment_account" if "Выписка по платёжному счёту" in text else "unknown")
    card_match = _CARD_RE.search(text)
    card = re.sub(r"\s+", "", card_match.group(1)) if card_match else None
    currency = "RUB" if "Российский рубль" in text else None
    opening_match = re.search(r"Остаток на\s+\d{2}\.\d{2}\.\d{4}\s+(" + _MONEY + r")", text)
    closing_matches = list(re.finditer(r"Остаток на\s+\d{2}\.\d{2}\.\d{4}\s+(" + _MONEY + r")", text))
    opening = _money(opening_match.group(1)) if opening_match else None
    closing = _money(closing_matches[-1].group(1)) if closing_matches else None
    inflow = _amount_after(text, "Пополнение") or _summary_amount(text, "Пополнение", "Списание")
    outflow = _amount_after(text, "Списание")
    if period_match is None:
        period = None
    else:
        period = [_date(period_match.group(1)), _date(period_match.group(2))]
    mapping = local_mapping or {}
    return {
        "bank_account": account,
        "account_type": statement_type,
        "card": card,
        "currency": currency,
        "period": period,
        "opening": str(opening) if opening is not None else None,
        "closing": str(closing) if closing is not None else None,
        "declared_inflow": str(inflow) if inflow is not None else None,
        "declared_outflow": str(outflow) if outflow is not None else None,
        "local_account": mapping.get(account) if account else None,
        "mapping_status": "mapped" if account in mapping else "manual_review",
    }


def build_statement_preview(path: Path, local_mapping: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a deterministic, non-mutating preview for one bank statement."""
    data = path.read_bytes()
    metadata = extract_statement_metadata(path, local_mapping)
    account = metadata["bank_account"] or "unresolved"
    parsed = read_sber_export(path, account=account)
    amounts = [tx.amount for tx in parsed.transactions]
    inflow = sum((amount for amount in amounts if amount > 0), Decimal(0))
    outflow = sum((-amount for amount in amounts if amount < 0), Decimal(0))
    opening = Decimal(metadata["opening"]) if metadata["opening"] is not None else None
    closing = Decimal(metadata["closing"]) if metadata["closing"] is not None else None
    movement = inflow - outflow
    discrepancy = closing - opening - movement if opening is not None and closing is not None else None
    issues: list[str] = []
    if metadata["mapping_status"] != "mapped":
        issues.append("bank_account_mapping_requires_manual_review")
    if metadata["currency"] != "RUB":
        issues.append("currency_unresolved")
    if metadata["declared_inflow"] is None or Decimal(metadata["declared_inflow"]) != inflow:
        issues.append("declared_inflow_mismatch")
    if metadata["declared_outflow"] is None or Decimal(metadata["declared_outflow"]) != outflow:
        issues.append("declared_outflow_mismatch")
    if discrepancy is None:
        issues.append("balance_pair_missing")
    elif discrepancy != 0:
        issues.append("balance_invariant_mismatch")
    return {
        "status": "BLOCKED" if issues else "checks_passed_read_only",
        "source": {"path_name": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                   "bytes": len(data), "format": path.suffix.lower().lstrip(".")},
        "metadata": metadata,
        "reconciliation": {"opening": str(opening) if opening is not None else None,
                            "closing": str(closing) if closing is not None else None,
                            "inflow": str(inflow), "outflow": str(outflow),
                            "movement": str(movement),
                            "discrepancy": str(discrepancy) if discrepancy is not None else None,
                            "status": "reconciled" if discrepancy == 0 else "mismatch" if discrepancy is not None else "unavailable",
                            "operation_count": len(parsed.transactions)},
        "parser": {"source_format": parsed.source_format, "warning_count": len(parsed.warnings),
                   "warnings": parsed.warnings},
        "issues": issues, "database_modified": False,
        "transactions": [transaction_payload(tx) for tx in parsed.transactions],
    }


def write_folder_intake(folder: Path, output_dir: Path, local_mapping: dict[str, str] | None = None) -> dict[str, Any]:
    """Write scopes and previews for all PDFs into a separate output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(folder.glob("*.pdf")):
        preview = build_statement_preview(path, local_mapping)
        stem = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        (output_dir / f"{stem}.scope.json").write_text(json.dumps(preview["metadata"], ensure_ascii=False, indent=2), encoding="utf-8")
        (output_dir / f"{stem}.preview.json").write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append({"file": path.name, "scope": f"{stem}.scope.json", "preview": f"{stem}.preview.json",
                        "status": preview["status"], "issues": preview["issues"]})
    manifest = {"status": "BLOCKED" if any(item["status"] == "BLOCKED" for item in results) else "checks_passed_read_only",
                "files": results, "database_modified": False}
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
