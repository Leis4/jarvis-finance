from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from jarvis.finance.db import FinanceDB


@dataclass(frozen=True)
class QualityIssue:
    severity: str
    code: str
    message: str
    count: int = 1


@dataclass(frozen=True)
class QualityReport:
    issues: list[QualityIssue]

    @property
    def ok(self) -> bool:
        return not any(issue.severity in {"error", "critical"} for issue in self.issues)


def _to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def validate_finance_db(db: FinanceDB, today: date | None = None) -> QualityReport:
    today = today or date.today()  # noqa: DTZ011
    issues: list[QualityIssue] = []
    txs = db.all_transactions()

    if not txs:
        issues.append(QualityIssue("warning", "no_transactions", "Нет транзакций: прогнозы и оптимизация будут слабыми."))

    future = 0
    invalid_amount = 0
    uncategorized_expenses = 0
    income_negative = 0
    huge_expenses = 0
    for tx in txs:
        try:
            tx_date = date.fromisoformat(str(tx["tx_date"]))
            if tx_date > today + timedelta(days=1):
                future += 1
        except ValueError:
            issues.append(QualityIssue("error", "bad_tx_date", f"Некорректная дата транзакции: {tx.get('tx_date')}"))
        amount = _to_decimal(tx.get("amount"))
        if amount is None:
            invalid_amount += 1
            continue
        category = str(tx.get("category") or "")
        if category == "income" and amount < 0:
            income_negative += 1
        if amount < 0 and category in {"", "uncategorized"}:
            uncategorized_expenses += 1
        if amount < Decimal(-100000):
            huge_expenses += 1

    if future:
        issues.append(QualityIssue("error", "future_transactions", "Есть транзакции с датой далеко в будущем.", future))
    if invalid_amount:
        issues.append(QualityIssue("error", "invalid_amount", "Есть транзакции с некорректной суммой.", invalid_amount))
    if income_negative:
        issues.append(QualityIssue("warning", "negative_income", "Есть income-транзакции с отрицательной суммой.", income_negative))
    if uncategorized_expenses:
        issues.append(QualityIssue("warning", "uncategorized_expenses", "Есть расходы без категории: ML и отчеты будут хуже.", uncategorized_expenses))
    if huge_expenses:
        issues.append(QualityIssue("warning", "huge_expenses", "Есть очень крупные расходы: проверь, не ошибка ли это.", huge_expenses))

    latest = db.latest_budget_snapshot()
    if latest:
        for field in ("cash_balance", "reserve_balance", "savings_balance"):
            val = _to_decimal(latest.get(field))
            if val is None:
                issues.append(QualityIssue("error", "bad_snapshot_balance", f"Некорректное поле snapshot: {field}"))
            elif val < 0:
                issues.append(QualityIssue("warning", "negative_snapshot_balance", f"Отрицательный баланс в snapshot: {field}"))
    else:
        issues.append(QualityIssue("info", "no_snapshot", "Нет фактического snapshot остатков: добавь add-snapshot для точного планирования."))

    return QualityReport(issues)


def render_quality_report(report: QualityReport) -> str:
    if not report.issues:
        return "Проверка данных: OK. Критичных проблем не найдено."
    lines = [f"Проверка данных: {'OK' if report.ok else 'есть ошибки'}", ""]
    for issue in report.issues:
        suffix = f" x{issue.count}" if issue.count != 1 else ""
        lines.append(f"- [{issue.severity}] {issue.code}{suffix}: {issue.message}")
    return "\n".join(lines)


def build_data_quality_dashboard(db: FinanceDB) -> dict[str, Any]:
    """User-facing data quality snapshot for the modern UI."""
    txs = db.all_transactions()
    total = len(txs)
    expenses = [tx for tx in txs if (amt := _to_decimal(tx.get("amount"))) is not None and amt < 0]
    income = [tx for tx in txs if (amt := _to_decimal(tx.get("amount"))) is not None and amt > 0]
    uncategorized = [tx for tx in expenses if str(tx.get("category") or "") in {"", "uncategorized", "transfer", "income_transfer"}]
    high_conf = max(0, total - len(uncategorized))
    latest_snapshot = db.latest_budget_snapshot()
    import_files: list[dict[str, Any]] = []
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT file_path, source_format, parsed_count, inserted_count, created_at FROM sber_import_files ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            import_files = [dict(row) for row in rows]
    except Exception:  # noqa: BLE001
        import_files = []
    quality_setting = db.get_setting("category_model_quality", {})
    warnings: list[str] = []
    if total == 0:
        warnings.append("Загрузи PDF/CSV из Сбера: пока нет операций для анализа.")
    if not latest_snapshot:
        warnings.append("Нет актуальной справки остатков: план может быть неточным.")
    if uncategorized:
        warnings.append(f"Есть спорные/неразобранные расходы: {len(uncategorized)}. Открой страницу проверки операций.")
    if quality_setting.get("accuracy") is not None and float(quality_setting.get("accuracy") or 0) < 0.70:
        warnings.append("Точность модели ниже 70%: нужно больше ручных исправлений перед автокатегоризацией.")
    return {
        "transactions_total": total,
        "expenses_total": len(expenses),
        "income_total": len(income),
        "high_confidence_estimate": high_conf,
        "manual_review_estimate": len(uncategorized),
        "recognized_percent": round((high_conf / total * 100), 1) if total else 0.0,
        "latest_snapshot": latest_snapshot,
        "import_files": import_files,
        "model_quality": quality_setting,
        "warnings": warnings,
        "issues": validate_finance_db(db).issues,
    }


def render_data_quality_dashboard_text(dashboard: dict[str, Any]) -> str:
    lines = [
        "Качество данных",
        f"Операций всего: {dashboard['transactions_total']}",
        f"Расходов: {dashboard['expenses_total']}",
        f"Поступлений: {dashboard['income_total']}",
        f"Оценка уверенно распознано: {dashboard['high_confidence_estimate']} ({dashboard['recognized_percent']}%)",
        f"Оценка на проверку: {dashboard['manual_review_estimate']}",
    ]
    snap = dashboard.get("latest_snapshot")
    if snap:
        lines.append(f"Последний snapshot: {snap.get('snapshot_date')} | main={snap.get('cash_balance')} reserve={snap.get('reserve_balance')} savings={snap.get('savings_balance')}")
    else:
        lines.append("Последний snapshot: отсутствует")
    mq = dashboard.get("model_quality") or {}
    if mq:
        lines.append(f"ML quality: accuracy={mq.get('accuracy')} validation_samples={mq.get('validation_samples')}")
    files = dashboard.get("import_files") or []
    lines.append(f"Импортированных файлов: {len(files)}")
    for warning in dashboard.get("warnings") or []:
        lines.append(f"WARNING: {warning}")
    return "\n".join(lines)
