"""Minimal persistent event coalescing; never dispatches a model."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from jarvis.finance.review_store import ReviewStore

MAX_QUEUE = 50
COOLDOWN_SECONDS = 300


def _path(store: ReviewStore) -> Path:
    return store.workspace / 'cognitive-events.json'


def _load(store: ReviewStore) -> list[dict[str, Any]]:
    try:
        value = json.loads(_path(store).read_text(encoding='utf-8'))
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def _save(store: ReviewStore, queue: list[dict[str, Any]]) -> None:
    path = _path(store)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(queue, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def emit(store: ReviewStore, event_type: str, *, key: str, revision: str,
         now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else now
    queue = _load(store)
    for item in queue:
        if (item['event_type'] == event_type and item['key'] == key
                and item['revision'] == revision):
            return {'status': 'coalesced', 'event_id': item['event_id']}
        if (item['event_type'] == event_type and item['key'] == key
                and current - item['observed_at'] < COOLDOWN_SECONDS):
            return {'status': 'cooldown', 'event_id': item['event_id']}
    event_id = f'{event_type}:{key}:{revision}'
    queue.append({'event_id': event_id, 'event_type': event_type, 'key': key,
                  'revision': revision, 'observed_at': current, 'status': 'pending'})
    _save(store, queue[-MAX_QUEUE:])
    return {'status': 'queued', 'event_id': event_id}


def pending(store: ReviewStore) -> list[dict[str, Any]]:
    return [item for item in _load(store) if item.get('status') == 'pending']


def mark(store: ReviewStore, event_id: str, status: str = 'processed') -> None:
    if status not in {'processed', 'skipped'}:
        raise ValueError('Invalid event status')
    queue = _load(store)
    for item in queue:
        if item['event_id'] == event_id:
            item['status'] = status
            break
    _save(store, queue)
