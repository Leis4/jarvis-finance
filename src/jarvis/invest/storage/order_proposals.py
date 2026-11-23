from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.utils import utc_now


def _root_dir() -> Path:
    return get_settings().artifacts_dir / "order_proposals"


def _pending_dir() -> Path:
    path = _root_dir() / "pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _archive_dir() -> Path:
    path = _root_dir() / "archive"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _proposal_path(proposal_id: str, *, status: str = "pending") -> Path:
    safe = str(proposal_id).strip()
    if not safe:
        safe = uuid4().hex
    if status == "pending":
        return _pending_dir() / f"proposal_{safe}.json"
    return _archive_dir() / f"proposal_{safe}_{status}.json"


def create_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    proposal_id = uuid4().hex
    created_at = utc_now().isoformat()
    stored = dict(payload)
    stored["proposal_id"] = proposal_id
    stored["status"] = "pending"
    stored["created_at"] = created_at
    stored.setdefault("events", []).append({"event": "created", "at": created_at})
    path = _proposal_path(proposal_id, status="pending")
    path.write_text(json.dumps(stored, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return {"proposal_id": proposal_id, "status": "pending", "created_at": created_at, "path": str(path)}


def load_proposal(proposal_id: str) -> dict[str, Any]:
    path = _proposal_path(proposal_id, status="pending")
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data) if isinstance(data, dict) else {}


def archive_proposal(proposal_id: str, *, status: str, actor: str, note: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    current = load_proposal(proposal_id)
    if not current:
        return {}
    archived = dict(current)
    archived["status"] = status
    event: dict[str, Any] = {
        "event": status,
        "at": utc_now().isoformat(),
        "actor": actor,
    }
    if note:
        event["note"] = str(note)
    if extra:
        event["extra"] = dict(extra)
    archived.setdefault("events", []).append(event)
    dst = _proposal_path(proposal_id, status=status)
    dst.write_text(json.dumps(archived, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    try:
        _proposal_path(proposal_id, status="pending").unlink(missing_ok=True)
    except OSError:
        pass
    return {"proposal_id": proposal_id, "status": status, "path": str(dst)}


def list_pending(limit: int = 20) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(_pending_dir().glob("proposal_*.json"), reverse=True)[: max(1, int(limit or 20))]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"status": "corrupted", "path": str(path)}
        payload.setdefault("path", str(path))
        items.append(payload)
    return items


def list_archive(limit: int = 20) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    paths = list(_archive_dir().glob("proposal_*.json"))
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[: max(1, int(limit or 20))]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"status": "corrupted", "path": str(path)}
        payload.setdefault("path", str(path))
        items.append(payload)
    return items
