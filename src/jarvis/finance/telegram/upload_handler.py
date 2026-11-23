"""Handle PDF uploads from Telegram."""
import hashlib
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.finance.bank import sber_statement
from jarvis.finance.production_intake import build_statement_preview
from jarvis.finance.telegram import upload_session
from jarvis.finance.telegram.pdf_classifier import PdfType, classify_pdf, compute_hash

logger = logging.getLogger(__name__)

STAGING_DIR = Path("telegram_staging")
STAGING_DIR.mkdir(exist_ok=True)
QUARANTINE_DIR = STAGING_DIR / "quarantine"

TYPE_LABELS = {
    PdfType.BALANCES: "📋 Справка об остатках",
    PdfType.SAVINGS: "💰 Сберегательный счёт",
    PdfType.MAIN: "💳 Основной счёт (••0090)",
    PdfType.RESERVE: "💳 Резервный счёт (••6342)",
    PdfType.ADDITIONAL: "💳 Дополнительный счёт",
}


async def _reply(update: Any, text: str) -> None:
    if hasattr(update, "message") and hasattr(update.message, "reply_text"):
        res = update.message.reply_text(text)
        if hasattr(res, "__await__"):
            await res
    elif hasattr(update, "reply_text"):
        res = update.reply_text(text)
        if hasattr(res, "__await__"):
            await res


def _extract_user_id(update: Any) -> int:
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    if user_id is None and hasattr(update, "message") and hasattr(update.message, "from_user"):
        user_id = getattr(update.message.from_user, "id", 1)
    if user_id is None:
        user_id = 1
    return int(user_id)


async def handle_document(update: Any, context: Any = None) -> None:
    user_id = _extract_user_id(update)

    msg = getattr(update, "message", update)
    doc = getattr(msg, "document", None)
    if not doc:
        return

    raw_filename = getattr(doc, "file_name", "unknown.pdf") or "unknown.pdf"
    filename = raw_filename.replace("\xa0", " ").replace("\u202f", " ").replace("/", "_").replace("\\", "_")
    if not filename.lower().endswith(".pdf"):
        await _reply(update, "❌ Только PDF. Попробуй ещё раз.")
        return

    file_size = getattr(doc, "file_size", 0) or 0
    if file_size > 20 * 1024 * 1024:
        await _reply(update, "❌ Файл слишком большой (макс 20 MB).")
        return

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    dest = STAGING_DIR / f"{ts}_{filename}"

    if hasattr(doc, "get_file"):
        file_obj = await doc.get_file() if callable(doc.get_file) else doc.get_file
        if hasattr(file_obj, "download_to_drive"):
            res = file_obj.download_to_drive(dest)
            if hasattr(res, "__await__"):
                await res
        elif hasattr(file_obj, "download"):
            res = file_obj.download(custom_path=dest)
            if hasattr(res, "__await__"):
                await res
    elif hasattr(doc, "save_to"):
        res = doc.save_to(dest)
        if hasattr(res, "__await__"):
            await res
    else:
        if not dest.exists():
            dest.write_bytes(getattr(doc, "content", b"fake pdf content"))

    classified = classify_pdf(dest)

    if classified.pdf_type == PdfType.UNKNOWN:
        dest.unlink(missing_ok=True)
        await _reply(update, "❌ Не удалось распознать тип PDF файла. Ожидаются выписки Сбербанка.")
        return

    session = upload_session.get_or_create(user_id)

    # Hash duplicate check
    if any(existing.sha256 == classified.sha256 for existing in session.files.values()):
        dest.unlink(missing_ok=True)
        await _reply(update, "⚠️ Этот файл уже загружен.")
        return

    # Type duplicate check
    if classified.pdf_type in session.files:
        dest.unlink(missing_ok=True)
        label = TYPE_LABELS.get(classified.pdf_type, classified.pdf_type.value)
        await _reply(update, f"⚠️ Уже получен {label}.")
        return

    # Use the same read-only intake pipeline as local folder processing.
    preview_note = ""
    try:
        preview = build_statement_preview(dest)
        reconciliation = preview["reconciliation"]
        preview_note = (
            f"\n🔎 Preview: {preview['status']}; "
            f"баланс {reconciliation['status']}, расхождение {reconciliation['discrepancy']}"
        )
    except Exception as exc:  # noqa: BLE001
        preview_note = "\n🔎 Preview недоступен: " + type(exc).__name__ + "; документ требует проверки."
        logger.warning("Statement preview unavailable before review: %s", type(exc).__name__)

    session.files[classified.pdf_type] = classified
    label = TYPE_LABELS.get(classified.pdf_type, classified.pdf_type.value)

    if session.is_complete:
        await _reply(
            update,
            f"✅ {label} принят.{preview_note}\n📦 Загружено: 4/4 ✅\nОтправь /process для read-only проверки."
        )
    else:
        missing_labels = ", ".join(TYPE_LABELS[t] for t in session.missing)
        await _reply(
            update,
            f"✅ {label} принят.{preview_note}\n📦 Загружено: {session.count}/4\nНе хватает: {missing_labels}\nОтправь остальные или /process для read-only проверки."
        )


async def process_command(update: Any, context: Any = None, force: bool = False) -> None:
    user_id = _extract_user_id(update)
    session = upload_session.get(user_id)

    if not session or not session.files:
        await _reply(update, "❌ Нет файлов. Отправь PDF.")
        return

    if not session.is_complete and not force:
        missing_labels = ", ".join(TYPE_LABELS[t] for t in session.missing)
        await _reply(
            update,
            f"⚠️ Загружены не все 4 файла ({session.count}/4).\n"
            f"Не хватает: {missing_labels}\n"
            f"Отправь отсутствующие PDF или используй /force_process для частичного preview."
        )
        return

    if any(not Path(classified.path).is_file() for classified in session.files.values()):
        await _reply(update, "❌ Файл загрузки отсутствует. Загрузите выписки повторно; БД не изменена.")
        return
    if any(compute_hash(Path(classified.path)) != classified.sha256 for classified in session.files.values()):
        await _reply(update, "❌ Файл загрузки изменён. Загрузите выписки повторно; БД не изменена.")
        return
    parser_version = "sber-pdf:" + hashlib.sha256(Path(sber_statement.__file__).read_bytes()).hexdigest()
    results: list[dict[str, Any]] = []
    for classified in session.files.values():
        try:
            preview = build_statement_preview(Path(classified.path))
            result = {"source_sha256": classified.sha256, "parser_version": parser_version,
                      "status": preview["status"], "issues": preview["issues"],
                      "metadata": preview["metadata"], "reconciliation": preview["reconciliation"],
                      "database_modified": False}
        except Exception as exc:  # noqa: BLE001
            result = {"source_sha256": classified.sha256, "parser_version": parser_version,
                      "status": "BLOCKED", "issues": ["parser_unavailable:" + type(exc).__name__],
                      "database_modified": False}
        results.append(result)

    # A hash-addressed quarantine is evidence, not an event decision or an import queue.
    source_dir = QUARANTINE_DIR / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    for classified in session.files.values():
        target = source_dir / f"{classified.sha256}.pdf"
        if target.exists() and compute_hash(target) != classified.sha256:
            await _reply(update, "❌ Конфликт сохранённого источника; review BLOCKED, БД не изменена.")
            return
        if not target.exists():
            shutil.copy2(classified.path, target)
    batch_key = hashlib.sha256((parser_version + "|" +
                                "|".join(sorted(item["source_sha256"] for item in results))).encode()).hexdigest()
    manifest_path = QUARANTINE_DIR / f"{batch_key}.json"
    manifest = {"status": "BLOCKED", "apply_allowed": False, "database_modified": False,
                "reason": "Account mapping and legacy reconciliation require separate human review",
                "sources": results}
    if manifest_path.exists():
        try:
            if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
                raise ValueError("Existing manifest differs")
        except (OSError, ValueError) as exc:
            await _reply(update, f"❌ Конфликт review manifest: {type(exc).__name__}; БД не изменена.")
            return
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    upload_session.clear(user_id)
    errors = sum(bool(item["issues"]) for item in results)
    reasons = ", ".join(sorted({issue for item in results for issue in item["issues"]}))
    await _reply(update, f"🔎 Read-only preview: {len(results)} источников, {errors} требуют проверки. "
                 f"Review BLOCKED; пакет {batch_key[:12]}, apply_allowed=false, database_modified=false. "
                 f"Причины: {reasons or 'separate_manual_reconciliation'}. "
                 "PDF сохранены в карантине для отдельной сверки; операции не импортированы.")


async def force_process_command(update: Any, context: Any = None) -> None:
    await process_command(update, context=context, force=True)


async def reset_command(update: Any, context: Any = None) -> None:
    user_id = _extract_user_id(update)
    session = upload_session.get(user_id)

    if session and session.files:
        count = session.count
        for classified in list(session.files.values()):
            try:
                Path(classified.path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001, S110
                pass
        upload_session.clear(user_id)
        await _reply(update, f"🗑️ Сброшено {count} файлов.")
    else:
        await _reply(update, "Нечего сбрасывать.")


async def archive_info_command(update: Any, context: Any = None) -> None:
    archive = Path("sber_archive")
    if not archive.exists():
        await _reply(update, "📂 Архив пуст.")
        return

    lines = ["📂 Архив PDF:"]
    total_files = 0
    total_size = 0
    dirs = [d for d in sorted(archive.iterdir(), reverse=True) if d.is_dir()]
    if not dirs:
        await _reply(update, "📂 Архив пуст.")
        return

    for month_dir in dirs:
        files = list(month_dir.glob("*.pdf"))
        size = sum(f.stat().st_size for f in files)
        total_files += len(files)
        total_size += size
        lines.append(f"  {month_dir.name}: {len(files)} файлов ({size // 1024} KB)")

    lines.append(f"\nВсего: {total_files} файлов ({total_size // 1024 // 1024} MB)")
    await _reply(update, "\n".join(lines))


async def help_command(update: Any, context: Any = None) -> None:
    await _reply(
        update,
        "📖 Команды:\n"
        "/balance — баланс\n"
        "/report — отчёт\n"
        "/ml-info — статус AI\n"
        "/process — обработать PDF\n"
        "/force_process — частичный read-only preview\n"
        "/reset — сбросить\n"
        "/archive-info — архив\n"
        "/help — справка\n\n"
        "📁 Отправь PDF-выписки, потом /process для read-only проверки"
    )
