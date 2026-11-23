"""Back up private review evidence; restore only into a new workspace."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_sources import source_registry
from jarvis.finance.review_store import ReviewStore, encoded


def _verify(store: ReviewStore) -> dict[str, Any]:
    with store.connect() as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Review SQLite integrity failure")
    events = store.audit()
    labels = HumanLabels(store).audit()
    source_registry(store)
    generated = json.loads(store.metadata()["packet"]).get("source_kind") == "generated_pdf"
    return {"packet_hash": store.metadata()["packet_hash"], **({"inline_generated_originals": True} if generated else {}), "items": len(store.items()),
            "review_events": len(events), "label_events": len(labels)}


@contextmanager
def review_snapshot(store: ReviewStore) -> Iterator[ReviewStore]:
    """Yield a verified consistent private snapshot, even with concurrent review."""
    if not store.path.is_file() or store.path.is_symlink():
        raise ValueError("Initialize a private review workspace first")
    with tempfile.TemporaryDirectory(prefix="jarvis-review-snapshot-") as temporary:
        snapshot = ReviewStore(Path(temporary))
        with (closing(sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)) as source,
              closing(sqlite3.connect(snapshot.path)) as destination):
            source.backup(destination)
        _verify(snapshot)
        yield snapshot


def create_review_backup(store: ReviewStore) -> dict[str, Any]:
    """Snapshot SQLite consistently, excluding tokens, PDFs and staging copies."""
    _verify(store)
    folder = store.workspace / "backups"
    if folder.is_symlink():
        raise ValueError("Backup folder cannot be a symlink")
    folder.mkdir(exist_ok=True)
    with review_snapshot(store) as snapshot:
        manifest = _verify(snapshot)
        data = snapshot.path.read_bytes()
        manifest.update({"format": "jarvis-private-review-v1", "created_at": datetime.now(UTC).isoformat(),
                         "sha256": hashlib.sha256(data).hexdigest(), "production_database_modified": False,
                         "database_modified": False,
                         "excluded": ["access_token", "original_pdf_files" if manifest.get("inline_generated_originals") else "original_pdfs", "staging_databases"]})
        archive = folder / (uuid.uuid4().hex + ".zip")
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr("review.sqlite", data)
            output.writestr("manifest.json", encoded(manifest))
    return {"archive": str(archive), **manifest}


def restore_review_backup(archive: Path, workspace: Path) -> dict[str, Any]:
    """Validate archive before creating a fresh directory; never overwrite data."""
    if workspace.exists() or workspace.is_symlink():
        raise ValueError("Restore requires a new workspace directory")
    with zipfile.ZipFile(archive) as source:
        if sorted(source.namelist()) != ["manifest.json", "review.sqlite"]:
            raise ValueError("Unexpected review archive entries")
        if any(info.file_size > 256 * 1024 * 1024 for info in source.infolist()):
            raise ValueError("Review archive entry exceeds size limit")
        manifest = json.loads(source.read("manifest.json"))
        data = source.read("review.sqlite")
    if manifest.get("format") != "jarvis-private-review-v1" or hashlib.sha256(data).hexdigest() != manifest.get("sha256"):
        raise ValueError("Review backup checksum or format failure")
    with tempfile.TemporaryDirectory(prefix="jarvis-review-restore-") as temporary:
        snapshot = ReviewStore(Path(temporary))
        snapshot.path.write_bytes(data)
        verified = _verify(snapshot)
        if any(manifest.get(key) != value for key, value in verified.items()):
            raise ValueError("Review backup manifest mismatch")
    workspace.mkdir(parents=True, exist_ok=False)
    with (workspace / "review.sqlite").open("xb") as output:
        output.write(data)
    return {"workspace": str(workspace.resolve()), **verified, "production_database_modified": False,
            "database_modified": False,
            "original_sources_required": not verified.get("inline_generated_originals", False), "staging_copies_restored": False}
