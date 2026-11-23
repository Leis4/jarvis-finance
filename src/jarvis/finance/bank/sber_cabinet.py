from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from jarvis.finance.bank.sber_ingestion import build_sber_preview, commit_sber_import
from jarvis.finance.bank.sber_statement import (
    SUPPORTED_EXTENSIONS,
    discover_sber_exports,
)
from jarvis.finance.db import FinanceDB


@dataclass(frozen=True)
class SberCabinetWorkflow:
    downloads_folder: str
    supported_formats: list[str]
    safe_commands: list[str]
    official_export_steps: list[str]
    safety_rules: list[str]


@dataclass(frozen=True)
class SberInboxItem:
    file_path: str
    source_format: str
    parsed_count: int
    duplicate_file: bool
    total_income: Decimal
    total_expense: Decimal
    warnings: list[str]


@dataclass(frozen=True)
class SberInboxReport:
    folder: str
    files_seen: int
    files_parseable: int
    new_files: int
    duplicate_files: int
    items: list[SberInboxItem]


@dataclass(frozen=True)
class SberBatchCommitReport:
    folder: str
    committed_files: int
    parsed_transactions: int
    inserted_transactions: int
    skipped_duplicates: int
    warnings: list[str]


def build_sber_cabinet_workflow(downloads_folder: str | Path = "./downloads") -> SberCabinetWorkflow:
    folder = str(Path(downloads_folder))
    return SberCabinetWorkflow(
        downloads_folder=folder,
        supported_formats=sorted(SUPPORTED_EXTENSIONS),
        official_export_steps=[
            "Открой СберБанк Онлайн самостоятельно — приложение или сайт.",
            "В поиске введи «Выписки и справки» и выбери нужный документ/счет/карту.",
            "Укажи период, закажи документ и скачай файл в локальную папку загрузок.",
            f"Положи файл в папку {folder} или укажи путь к нему напрямую.",
            "Запусти sber-inbox для предпросмотра, затем sber-import-new --commit для записи в SQLite.",
        ],
        safe_commands=[
            f"finance-agent --db ./finance.db sber-inbox --folder {folder}",
            f"finance-agent --db ./finance.db sber-import-new --folder {folder} --commit",
            "finance-agent --db ./finance.db math-forecast",
            "finance-agent --db ./finance.db control-report --days-to-income 14",
        ],
        safety_rules=[
            "Проект не хранит логин, пароль, cookies, SMS-коды или push-подтверждения Сбера.",
            "Проект не обходит 2FA и не управляет браузером в личном кабинете банка.",
            "Импорт идет только из файлов, которые пользователь сам получил в личном кабинете.",
            "Любое действие с деньгами остается pending action / ручным чеклистом, а не реальным переводом.",
        ],
    )


def scan_sber_inbox(db: FinanceDB, folder: str | Path, account: str = "sber", limit: int = 10) -> SberInboxReport:
    files = discover_sber_exports(folder)
    items: list[SberInboxItem] = []
    parseable = 0
    new_files = 0
    duplicate_files = 0
    for path in files:
        try:
            preview = build_sber_preview(db, path, account=account, limit=limit)
        except Exception as exc:  # noqa: BLE001 - inbox scan should keep going
            items.append(
                SberInboxItem(
                    file_path=str(path),
                    source_format="error",
                    parsed_count=0,
                    duplicate_file=False,
                    total_income=Decimal("0.00"),
                    total_expense=Decimal("0.00"),
                    warnings=[str(exc)],
                )
            )
            continue
        parseable += 1
        if preview.duplicate_file:
            duplicate_files += 1
        else:
            new_files += 1
        items.append(
            SberInboxItem(
                file_path=preview.file_path,
                source_format=preview.source_format,
                parsed_count=preview.parsed_count,
                duplicate_file=preview.duplicate_file,
                total_income=preview.total_income,
                total_expense=preview.total_expense,
                warnings=preview.warnings,
            )
        )
    return SberInboxReport(str(folder), len(files), parseable, new_files, duplicate_files, items)


def commit_new_sber_exports(db: FinanceDB, folder: str | Path, account: str = "sber") -> SberBatchCommitReport:
    files = discover_sber_exports(folder)
    committed_files = 0
    parsed_transactions = 0
    inserted_transactions = 0
    skipped_duplicates = 0
    warnings: list[str] = []
    for path in files:
        try:
            commit = commit_sber_import(db, path, account=account, force=False)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{path.name}: {exc}")
            continue
        if commit.skipped_duplicate_file:
            skipped_duplicates += 1
            continue
        committed_files += 1
        parsed_transactions += commit.parsed_count
        inserted_transactions += commit.inserted_count
        warnings.extend(f"{path.name}: {w}" for w in commit.warnings[:10])
    return SberBatchCommitReport(
        folder=str(folder),
        committed_files=committed_files,
        parsed_transactions=parsed_transactions,
        inserted_transactions=inserted_transactions,
        skipped_duplicates=skipped_duplicates,
        warnings=warnings,
    )


def render_sber_cabinet_workflow(workflow: SberCabinetWorkflow) -> str:
    lines = [
        "Sber cabinet safe-reading workflow",
        f"local downloads folder: {workflow.downloads_folder}",
        f"supported formats: {', '.join(workflow.supported_formats)}",
        "",
        "Official/manual export steps:",
    ]
    lines.extend(f"{idx}. {step}" for idx, step in enumerate(workflow.official_export_steps, start=1))
    lines.append("")
    lines.append("Safe commands:")
    lines.extend(f"- {cmd}" for cmd in workflow.safe_commands)
    lines.append("")
    lines.append("Safety boundaries:")
    lines.extend(f"- {rule}" for rule in workflow.safety_rules)
    return "\n".join(lines)


def render_sber_inbox(report: SberInboxReport) -> str:
    lines = [
        "Sber local inbox scan",
        f"folder: {report.folder}",
        f"files_seen: {report.files_seen}",
        f"files_parseable: {report.files_parseable}",
        f"new_files: {report.new_files}",
        f"duplicate_files: {report.duplicate_files}",
        "",
        "Files:",
    ]
    if not report.items:
        lines.append("- no supported files found")
        return "\n".join(lines)
    for item in report.items:
        lines.append(
            f"- {item.file_path} | format={item.source_format} | parsed={item.parsed_count} | "
            f"duplicate={item.duplicate_file} | income={item.total_income} | expenses={item.total_expense}"
        )
        for warning in item.warnings[:3]:
            lines.append(f"  warning: {warning}")
    return "\n".join(lines)


def render_sber_batch_commit(report: SberBatchCommitReport) -> str:
    lines = [
        "Sber batch import",
        f"folder: {report.folder}",
        f"committed_files: {report.committed_files}",
        f"parsed_transactions: {report.parsed_transactions}",
        f"inserted_transactions: {report.inserted_transactions}",
        f"skipped_duplicate_files: {report.skipped_duplicates}",
    ]
    if report.warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in report.warnings[:20])
    return "\n".join(lines)
