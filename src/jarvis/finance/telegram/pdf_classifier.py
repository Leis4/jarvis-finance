"""Classify Sber PDFs by content (not filename)."""
import hashlib
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class PdfType(str, Enum):
    BALANCES = "BALANCES"
    SAVINGS = "SAVINGS"
    MAIN = "MAIN"
    RESERVE = "RESERVE"
    ADDITIONAL = "ADDITIONAL"
    UNKNOWN = "UNKNOWN"


@dataclass
class ClassifiedPdf:
    path: Path
    sha256: str
    pdf_type: PdfType
    account_number: str | None
    period_start: str | None
    period_end: str | None
    month_key: str | None


def compute_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _to_iso(ddmmyyyy: str) -> str:
    d, m, y = ddmmyyyy.split(".")
    return f"{y}-{m}-{d}"


def _extract_text(path: Path) -> str:
    """Extract text using existing Sber parser or fallback."""
    try:
        from jarvis.finance.bank.sber_statement import _extract_pdf_text
        txt = _extract_pdf_text(path)
        if txt and len(txt.strip()) > 0:
            return txt
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        text = ""
        for page in reader.pages[:2]:
            text += page.extract_text() or ""
        if text.strip():
            return text
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def classify_pdf(path: Path) -> ClassifiedPdf:
    sha = compute_hash(path)
    text = _extract_text(path)
    text_lower = text.lower()

    pdf_type = PdfType.UNKNOWN
    account_number = None

    if "справка о доступном остатке" in text_lower or "доступном остатке" in text_lower:
        pdf_type = PdfType.BALANCES
    elif "выписка по вкладу" in text_lower or "выписка по вкладу или счёту" in text_lower or "0884890" in text:
        pdf_type = PdfType.SAVINGS
        account_number = "40817 810 4 6249 0884890"
    elif ("выписка по платёжному счёту" in text_lower 
          or "выписка по платежному счету" in text_lower
          or "8671688" in text or "0433295" in text):
        if "8671688" in text or "6200 8671688" in text:
            pdf_type = PdfType.MAIN
            account_number = "40817 810 0 6200 8671688"
        elif "0433295" in text or "6249 0433295" in text:
            pdf_type = PdfType.RESERVE
            account_number = "40817 810 1 6249 0433295"
        else:
            pdf_type = PdfType.ADDITIONAL
            match = re.search(r"Номер сч[её]та\s+((?:\d\s*){20})", text, re.IGNORECASE)
            account_number = match.group(1).strip() if match else None

    # Extract period
    m = re.search(r"За период\s+(\d{2}\.\d{2}\.\d{4})\s*[–-]\s*(\d{2}\.\d{2}\.\d{4})", text)
    period_start = period_end = month_key = None
    if m:
        period_start = _to_iso(m.group(1))
        period_end = _to_iso(m.group(2))
        month_key = period_start[:7]

    return ClassifiedPdf(
        path=path,
        sha256=sha,
        pdf_type=pdf_type,
        account_number=account_number,
        period_start=period_start,
        period_end=period_end,
        month_key=month_key,
    )
