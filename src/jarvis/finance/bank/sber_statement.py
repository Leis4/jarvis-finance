from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from jarvis.finance.bank.savings_validation import (
    savings_category,
    savings_document,
    zero_account_event,
)
from jarvis.finance.domain import Transaction
from jarvis.finance.importers import infer_category

DATE_ALIASES = {
    "date",
    "tx_date",
    "дата",
    "дата операции",
    "дата транзакции",
    "дата платежа",
    "дата списания",
    "operation date",
    "transaction date",
}
AMOUNT_ALIASES = {
    "amount",
    "сумма",
    "сумма операции",
    "сумма в валюте счета",
    "сумма платежа",
    "итого",
}
DEBIT_ALIASES = {"расход", "списание", "debit", "withdrawal", "outcome", "expense"}
CREDIT_ALIASES = {"приход", "зачисление", "credit", "income", "deposit"}
DESCRIPTION_ALIASES = {
    "description",
    "описание",
    "операция",
    "назначение платежа",
    "назначение",
    "детали операции",
    "комментарий",
    "transaction details",
}
MERCHANT_ALIASES = {"merchant", "мерчант", "получатель", "контрагент", "магазин", "место операции", "mcc name"}
CATEGORY_ALIASES = {"category", "категория", "категория операции"}
CURRENCY_ALIASES = {"currency", "валюта", "валюта операции", "валюта счета"}

DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%d.%m.%Y",
    "%d.%m.%Y %H:%M",
    "%d/%m/%Y",
    "%d/%m/%Y %H:%M",
)

SUPPORTED_EXTENSIONS = {".csv", ".txt", ".tsv", ".xlsx", ".pdf"}


@dataclass(frozen=True)
class SberParseResult:
    transactions: list[Transaction]
    warnings: list[str] = field(default_factory=list)
    source_format: str = "unknown"
    text: str = ""


@dataclass(frozen=True)
class SberImportSummary:
    files_seen: int
    files_imported: int
    inserted: int
    warnings: list[str]


def _norm_header(value: str | None) -> str:
    return " ".join(str(value or "").replace("\ufeff", "").strip().lower().split())


def _first_key(row: Mapping[str, object], aliases: set[str]) -> str | None:
    norm_to_key = {_norm_header(k): k for k in row}
    for alias in aliases:
        if alias in norm_to_key:
            return norm_to_key[alias]
    # Partial match for bank exports with long headings.
    for norm, key in norm_to_key.items():
        if any(alias in norm for alias in aliases if len(alias) >= 5):
            return key
    return None


def _value(row: Mapping[str, object], aliases: set[str], default: str = "") -> str:
    key = _first_key(row, aliases)
    if key is None:
        return default
    value = row.get(key, default)
    return "" if value is None else str(value).strip()


def parse_money(value: object) -> Decimal:
    raw = str(value or "").strip()
    if not raw:
        return Decimal("0.00")
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
    if raw.startswith("-"):
        negative = True
    cleaned = raw.replace("\xa0", " ").replace("₽", "").replace("руб.", "").replace("RUB", "")
    cleaned = cleaned.replace("руб", "").replace(" ", "").replace("+", "").replace("(", "").replace(")", "")
    cleaned = cleaned.replace("−", "-")
    if cleaned.count(",") == 1 and cleaned.count(".") == 0:
        cleaned = cleaned.replace(",", ".")
    if cleaned.count(",") == 1 and cleaned.count(".") >= 1:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        result = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse money value: {raw!r}") from exc
    if negative and result > 0:
        result = -result
    return result


def parse_sber_date(value: object) -> date:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty date")
    raw = raw.replace("\xa0", " ")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw[: len(datetime.now().strftime(fmt))], fmt).date()  # noqa: DTZ005, DTZ007
        except ValueError:
            pass
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise ValueError(f"Cannot parse date value: {raw!r}") from exc


def _external_id(account: str, tx_date: date, amount: Decimal, description: str, merchant: str) -> str:
    raw = f"sber|{account}|{tx_date.isoformat()}|{amount}|{description}|{merchant}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _row_to_transaction(row: Mapping[str, object], account: str) -> Transaction:
    tx_date = parse_sber_date(_value(row, DATE_ALIASES))
    amount_text = _value(row, AMOUNT_ALIASES)
    if amount_text:
        amount = parse_money(amount_text)
    else:
        debit = parse_money(_value(row, DEBIT_ALIASES))
        credit = parse_money(_value(row, CREDIT_ALIASES))
        amount = abs(credit) if credit else -abs(debit)
    description = _value(row, DESCRIPTION_ALIASES)
    merchant = _value(row, MERCHANT_ALIASES)
    if not description and merchant:
        description = merchant
    if not merchant and description:
        merchant = description.split("  ")[0][:80].strip()
    category = _value(row, CATEGORY_ALIASES).lower() or infer_category(description, merchant, amount)
    currency = _value(row, CURRENCY_ALIASES, "RUB").upper() or "RUB"
    return Transaction(
        tx_date=tx_date,
        amount=amount,
        currency=currency,
        description=description,
        merchant=merchant,
        category=category,
        account=account,
        external_id=_value(row, {"external_id", "id", "идентификатор операции"}) or _external_id(account, tx_date, amount, description, merchant),
    )


def parse_sber_csv(path: str | Path, account: str = "sber") -> SberParseResult:
    source = Path(path)
    warnings: list[str] = []
    transactions: list[Transaction] = []
    delimiter = "\t" if source.suffix.lower() == ".tsv" else ","
    with source.open("r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel_tab if delimiter == "\t" else csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("Sber export has no header row")
        if _first_key({h: "" for h in reader.fieldnames}, DATE_ALIASES) is None:
            raise ValueError("Sber export is missing a date column")
        if _first_key({h: "" for h in reader.fieldnames}, AMOUNT_ALIASES | DEBIT_ALIASES | CREDIT_ALIASES) is None:
            raise ValueError("Sber export is missing amount/debit/credit columns")
        for line_no, row in enumerate(reader, start=2):
            try:
                tx = _row_to_transaction(row, account=account)
                if tx.amount == Decimal("0.00"):
                    warnings.append(f"line {line_no}: skipped zero amount")
                    continue
                transactions.append(tx)
            except Exception as exc:  # noqa: BLE001 - return parse warnings, keep import robust
                warnings.append(f"line {line_no}: {exc}")
    return SberParseResult(transactions=transactions, warnings=warnings, source_format="csv")


TEXT_LINE_RE = re.compile(
    r"(?P<date>\d{2}[./]\d{2}[./]\d{4})\s+(?P<body>.+?)\s+(?P<amount>[+-]?[\d\s.,]+)\s*(?:₽|руб\.?|RUB)?$",
    re.IGNORECASE,
)


def parse_sber_text(path: str | Path, account: str = "sber") -> SberParseResult:
    source = Path(path)
    warnings: list[str] = []
    transactions: list[Transaction] = []
    for line_no, raw_line in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        match = TEXT_LINE_RE.search(line)
        if not match:
            # Ignore obvious headers instead of making pasted statements noisy.
            lowered = line.lower()
            if any(token in lowered for token in ("дата", "операция", "выписка", "сбер")):
                continue
            warnings.append(f"line {line_no}: skipped unrecognized text row")
            continue
        tx_date = parse_sber_date(match.group("date"))
        description = match.group("body").strip()
        amount = parse_money(match.group("amount"))
        merchant = description[:80]
        transactions.append(
            Transaction(
                tx_date=tx_date,
                amount=amount,
                currency="RUB",
                description=description,
                merchant=merchant,
                category=infer_category(description, merchant, amount),
                account=account,
                external_id=_external_id(account, tx_date, amount, description, merchant),
            )
        )
    return SberParseResult(transactions=transactions, warnings=warnings, source_format="text")


def parse_sber_xlsx(path: str | Path, account: str = "sber") -> SberParseResult:
    try:
        import openpyxl  # type: ignore
    except ImportError as exc:
        raise RuntimeError("XLSX import requires optional dependency: pip install -e '.[bank]'") from exc
    source = Path(path)
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return SberParseResult(transactions=[], warnings=["empty xlsx"], source_format="xlsx")
    headers = [str(cell or "").strip() for cell in rows[0]]
    warnings: list[str] = []
    transactions: list[Transaction] = []
    for idx, values in enumerate(rows[1:], start=2):
        row = {headers[i]: values[i] if i < len(values) else "" for i in range(len(headers))}
        if not any(str(v or "").strip() for v in row.values()):
            continue
        try:
            tx = _row_to_transaction(row, account=account)
            if tx.amount == Decimal("0.00"):
                warnings.append(f"line {idx}: skipped zero amount")
                continue
            transactions.append(tx)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"line {idx}: {exc}")
    return SberParseResult(transactions=transactions, warnings=warnings, source_format="xlsx")


PDF_TX_START_RE = re.compile(
    r"^(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<time>\d{2}:\d{2})\s+(?P<category>.+?)\s+(?P<amount>[+\-−]?[\d\s\xa0.,]+)$"
)
PDF_DETAIL_RE = re.compile(
    r"^(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<auth>[A-ZА-Я0-9]+)\s+(?P<description>.+)$"
)

PDF_MONEY_AT_END_RE = re.compile(r"[+\-−]?\d[\d\s\xa0]*[,.]\d{2}")


def _match_pdf_tx_line(line: str) -> dict[str, str] | None:
    head = re.match(r"^(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<time>\d{2}:\d{2})\s+(?P<rest>.+)$", line)
    if not head:
        return None
    rest = head.group("rest")
    matches = list(PDF_MONEY_AT_END_RE.finditer(rest))
    if not matches:
        return None
    # Statement variants either end with one amount or with amount + balance.
    last = matches[-1]
    prev = matches[-2] if len(matches) >= 2 and rest[last.end():].strip() == "" else None
    amount_match = prev if prev and prev.end() + 1 <= last.start() else last
    category = rest[: amount_match.start()].strip()
    if not category:
        return None
    return {"date": head.group("date"), "time": head.group("time"), "category": category, "amount": amount_match.group(0)}


def _match_pdf_savings_tx_line(line: str) -> dict[str, str] | None:
    """Match the date/description/amount/balance layout of savings statements."""
    head = re.match(r"^(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<rest>.+)$", line)
    if not head:
        return None
    if re.match(r"^\d{2}:\d{2}\s", head.group("rest")):
        return None
    matches = list(PDF_MONEY_AT_END_RE.finditer(head.group("rest")))
    if len(matches) < 2 or head.group("rest")[matches[-1].end():].strip():
        return None
    amount = matches[-2]
    category = head.group("rest")[: amount.start()].strip()
    if not category:
        return None
    return {"date": head.group("date"), "category": category, "amount": amount.group(0)}


PDF_SKIP_TOKENS = (
    "выписка",
    "действителен",
    "для проверки",
    "сбербанк онлайн",
    "итого по операциям",
    "расшифровка операций",
    "дата операции",
    "дата обработки",
    "категория",
    "описание операции",
    "сумма в валюте",
    "продолжение на следующей странице",
    "дата формирования",
    "генеральная лицензия",
    "денежные средства списываются",
    "срок обработки",
    "согласно статье",
)
PDF_INCOME_HINTS = (
    "перевод на карту",
    "зачисление",
    "пополнение",
    "внесение наличных",
    "возврат",
    "возврат покупки",
    "возврат, отмена операции",
    "перевод от",
    "vklad-karta",
    "вклад-карта",
    "перевод с брокерского счета",
)
PDF_TRANSFER_HINTS = (
    "перевод для г. пётр",
    "перевод от г. пётр",
    "sberbank onl@in karta-vklad",
    "sberbank onl@in vklad-karta",
    "альфа-банк",
    "t-банк",
    "т-банк",
    "ао \"t-банк\"",
    "yandexbank_c2a",
    "россельхоз",
    "rshb",
)


def _is_pdf_header_or_footer(line: str) -> bool:
    lowered = line.lower()
    if not line.strip():
        return True
    return any(token in lowered for token in PDF_SKIP_TOKENS)


def _signed_pdf_amount(raw_amount: str, category: str, description: str) -> Decimal:
    amount = abs(parse_money(raw_amount))
    blob = f"{category} {description}".lower()
    raw = raw_amount.strip().replace("−", "-")
    if raw.startswith("+"):
        return amount
    if raw.startswith("-"):
        return -amount
    if any(hint in blob for hint in PDF_INCOME_HINTS):
        return amount
    return -amount


def _category_from_sber_pdf(category: str, description: str, amount: Decimal) -> str:
    blob = f"{category} {description}".lower()
    if "ysplit" in blob or "splitmarket" in blob or "яндекс сплит" in blob:
        return "installment_yandex_split"
    if "yandex*5815*plus" in blob or "yandex plus" in blob:
        return "subscriptions"
    if "yandex*4121*go" in blob:
        return "taxi"
    if "scooters" in blob or "urent" in blob or "slidepoint" in blob:
        return "micromobility"
    if "karta-vklad" in blob or "карта-вклад" in blob:
        return "unknown"
    if any(hint in blob for hint in PDF_TRANSFER_HINTS):
        return "unknown"
    if "стип" in blob or "университет" in blob or "вуз" in blob:
        return "scholarship"
    if "транспорт" in blob or "mos.transport" in blob or "aeroekspress" in blob or "metro" in blob:
        return "transport"
    if "супермаркет" in blob or "пятер" in blob or "pyaterochka" in blob or "magnit" in blob or "магнит" in blob or "дикси" in blob or "lenta" in blob:
        return "food"
    if "перевод" in blob or "сбп" in blob:
        return "unknown"
    if "ресторан" in blob or "кафе" in blob or "burger" in blob or "шаур" in blob or "dodo" in blob or "vkusno" in blob or "еда" in blob or "edari" in blob or "lavka" in blob:
        return "delivery"
    if "yandex*5815*plus" in blob or "sbscr_vk" in blob or "koptevonet" in blob or "timeweb" in blob or "vpn" in blob:
        return "subscriptions"
    if "fitnes" in blob or "fitness" in blob:
        return "health"
    if "ozon" in blob or "wildberries" in blob or "market" in blob or "ysplit" in blob:
        return "shopping"
    if "funpay" in blob or "ggs" in blob or "playerok" in blob or "steam" in blob or "software" in blob or "robokassa" in blob:
        return "games"
    return infer_category(description, category, amount)


def parse_sber_pdf_text(text: str, account: str = "sber") -> SberParseResult:
    warnings: list[str] = []
    transactions: list[Transaction] = []
    lines = [" ".join(line.replace("\xa0", " ").strip().split()) for line in text.splitlines()]
    pending: dict[str, str] | None = None
    savings_pending: dict[str, str] | None = None
    is_savings_statement = savings_document(text)

    def flush_pending() -> None:
        nonlocal pending
        if pending is None:
            return
        description = pending.get("description", "").strip() or pending.get("category", "").strip()
        category_raw = pending.get("category", "")
        amount = _signed_pdf_amount(pending["amount"], category_raw, description)
        tx_date = parse_sber_date(pending["date"])
        merchant = description[:100]
        category = _category_from_sber_pdf(category_raw, description, amount)
        transactions.append(
            Transaction(
                tx_date=tx_date,
                amount=amount,
                currency="RUB",
                description=description,
                merchant=merchant,
                category=category,
                account=account,
                external_id=_external_id(account, tx_date, amount, description, merchant),
            )
        )
        pending = None

    for line_no, line in enumerate(lines, start=1):
        if _is_pdf_header_or_footer(line):
            continue
        if is_savings_statement and savings_pending is not None:
            money = list(PDF_MONEY_AT_END_RE.finditer(line))
            if len(money) >= 2 and not line[money[-1].end():].strip():
                description = savings_pending["category"]
                amount = _signed_pdf_amount(money[-2].group(0), description, description)
                tx_date = parse_sber_date(savings_pending["date"])
                transactions.append(Transaction(
                    tx_date=tx_date, amount=amount, currency="RUB", description=description,
                    merchant=description[:100], category=savings_category(description, amount),
                    account=account,
                    external_id=_external_id(account, tx_date, amount, description, description[:100]),
                ))
                savings_pending = None
                continue
            if re.match(r"^\d{2}\.\d{2}\.\d{4}\s", line):
                warnings.append(f"line {line_no}: savings operation has no amount")
                savings_pending = None
        savings_match = _match_pdf_savings_tx_line(line) if is_savings_statement else None
        if savings_match:
            flush_pending()
            savings_pending = savings_match
            continue
        savings_header = re.match(r"^(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<category>.+)$", line) if is_savings_statement else None
        if savings_header and not re.match(r"^\d{2}:\d{2}\s", savings_header.group("category")):
            lowered = savings_header.group("category").lower()
            if not lowered.startswith(("за период", "дата формирования", "страница", "остаток на")):
                flush_pending()
                savings_pending = savings_header.groupdict()
                continue
        tx_match = _match_pdf_tx_line(line)
        if tx_match:
            flush_pending()
            pending = tx_match
            continue
        detail_match = PDF_DETAIL_RE.match(line)
        if detail_match and pending is not None:
            pending["description"] = detail_match.group("description").strip()
            flush_pending()
            continue
        if pending is not None and not line.lower().startswith(("страница", "операции2")):
            if pending.get("description"):
                pending["description"] += " " + line
            else:
                pending["description"] = line
            continue
        lowered = line.lower()
        if any(token in lowered for token in ("номер счета", "карты, привязанные", "валюта", "пополнение", "списание", "остаток")):
            continue
        if re.search(r"\d{2}\.\d{2}\.\d{4}", line):
            warnings.append(f"line {line_no}: skipped unrecognized Sber PDF row: {line[:120]}")
    flush_pending()
    if is_savings_statement:
        metadata = [tx for tx in transactions if zero_account_event(tx.description, tx.amount)]
        transactions = [tx for tx in transactions if tx not in metadata]
        if metadata:
            warnings.append(f'{len(metadata)} zero account lifecycle event(s); not monetary transactions')
    return SberParseResult(transactions=transactions, warnings=warnings, source_format="pdf", text=text)


def _extract_pdf_text(source: Path) -> str:
    text = ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(source))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if len(text.strip()) >= 20:
            return text
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        import fitz  # type: ignore
        doc = fitz.open(str(source))
        text = "\n".join(page.get_text("text") for page in doc)
        if len(text.strip()) >= 20:
            return text
    except ImportError as exc:
        raise RuntimeError("PDF import requires optional dependency: pypdf or PyMuPDF") from exc

    return text


def parse_sber_pdf(path: str | Path, account: str = "sber") -> SberParseResult:
    source = Path(path)
    text = _extract_pdf_text(source)
    if not text.strip():
        return SberParseResult([], ["PDF не содержит извлекаемого текста. OCR fallback не сработал или не установлен; скачай цифровой PDF либо установи optional OCR extras."], "pdf")
    return parse_sber_pdf_text(text, account=account)


def read_sber_export(path: str | Path, account: str = "sber") -> SberParseResult:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Sber export not found: {source}")
    suffix = source.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        result = parse_sber_csv(source, account=account)
    elif suffix == ".txt":
        result = parse_sber_text(source, account=account)
    elif suffix == ".xlsx":
        result = parse_sber_xlsx(source, account=account)
    elif suffix == ".pdf":
        result = parse_sber_pdf(source, account=account)
    else:
        raise ValueError(f"Unsupported Sber export format: {suffix}. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
    return result


def discover_sber_exports(folder: str | Path) -> list[Path]:
    root = Path(folder)
    if not root.exists():
        raise FileNotFoundError(f"Folder not found: {root}")
    return sorted(path for path in root.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS)
