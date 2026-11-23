"""Auto-import Sber PDFs from watched folder."""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from jarvis.finance.bank.sber_ingestion import commit_sber_import
from jarvis.finance.db import FinanceDB

logger = logging.getLogger(__name__)


class SberInboxHandler(FileSystemEventHandler):
    """Watches sber_inbox/ and auto-imports new PDFs."""

    def __init__(
        self,
        db_path: str,
        inbox_dir: Path,
        archive_dir: Path,
        on_import_callback: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.db_path = db_path
        self.inbox_dir = inbox_dir
        self.archive_dir = archive_dir
        self.on_import = on_import_callback
        self._processing: set[str] = set()

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_event(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle_event(event)

    def _handle_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src_path = getattr(event, "src_path", "")
        if isinstance(src_path, bytes):
            src_path = src_path.decode("utf-8", errors="ignore")
        path = Path(str(src_path))
        if path.suffix.lower() not in (".pdf", ".csv", ".xlsx"):
            return
        # Debounce — file might still be writing
        time.sleep(2)
        self._process_file(path)

    def _process_file(self, path: Path) -> None:
        if str(path) in self._processing or not path.exists():
            return
        self._processing.add(str(path))

        try:
            print(f"[INBOX] New file: {path.name}")
            db = FinanceDB(self.db_path)
            db.init()
            result: Any = commit_sber_import(db, path)

            inserted = (
                result.get("inserted", 0)
                if isinstance(result, dict)
                else getattr(result, "inserted_count", 0)
            )

            # Archive processed file
            archive = self.archive_dir / datetime.now(UTC).strftime("%Y%m")
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(archive / path.name))

            print(f"[INBOX] Imported {inserted} transactions from {path.name}")

            # Optional: send Telegram alert
            if self.on_import and inserted > 0:
                self.on_import(f"📥 Импортировано {inserted} транзакций из {path.name}")
        except Exception as e:
            logger.exception("Failed to import %s", path)
            print(f"[INBOX] ERROR: {e}")
        finally:
            self._processing.discard(str(path))


def start_watcher(
    db_path: str,
    inbox_dir: str = "sber_inbox",
    archive_dir: str = "sber_archive",
    telegram_callback: Callable[[str], None] | None = None,
) -> Any:
    """Start watching folder in background. Returns observer."""
    inbox = Path(inbox_dir)
    archive = Path(archive_dir)
    inbox.mkdir(exist_ok=True)
    archive.mkdir(exist_ok=True)

    handler = SberInboxHandler(db_path, inbox, archive, telegram_callback)
    observer = Observer()
    observer.schedule(handler, str(inbox), recursive=False)
    observer.start()
    print(f"[INBOX] Watching {inbox.resolve()}")
    return observer
