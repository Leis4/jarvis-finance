"""Phase 5: local insights, owner feedback and safe memory retrieval."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.finance.personal_briefing import daily_briefing
from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_store import ReviewStore
from jarvis.services.events import emit

ACTIONS = {'confirm', 'correct', 'reject', 'not_relevant', 'remind_later'}
SUPPRESSED = {'reject', 'not_relevant'}


def _state_path(store: ReviewStore) -> Path:
    return store.workspace / 'cognitive-insights.json'


def _load(store: ReviewStore) -> dict[str, Any]:
    path = _state_path(store)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {'revision': None, 'feedback': {}, 'insights': []}
    except (OSError, ValueError):
        return {'revision': None, 'feedback': {}, 'insights': []}


def _save(store: ReviewStore, state: dict[str, Any]) -> None:
    path = _state_path(store)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def ledger_revision(cfo: Any) -> str:
    with cfo.db.connect() as conn:
        rows = conn.execute("SELECT COALESCE(MAX(id),0), COUNT(*) FROM transactions").fetchone()
        corrections = conn.execute("SELECT COUNT(*) FROM category_corrections").fetchone()[0]
    return hashlib.sha256(f'{rows[0]}:{rows[1]}:{corrections}'.encode()).hexdigest()[:16]


def collect(cfo: Any, store: ReviewStore, *, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    revision = ledger_revision(cfo)
    briefing = daily_briefing(cfo)
    state = _load(store)
    feedback = state.get('feedback') or {}
    active: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for event in briefing.get('events') or []:
        ident = str(event.get('id') or '')
        if not ident:
            continue
        decision = feedback.get(ident) or {}
        action = decision.get('action')
        until = decision.get('until')
        expired = bool(until and datetime.fromisoformat(until) > current)
        rejected = action in SUPPRESSED and decision.get('revision') == revision
        if rejected or expired:
            suppressed.append({'insight_id': ident, 'reason': action or 'remind_later'})
            continue
        active.append({
            'insight_id': ident,
            'insight_type': event.get('insight_type') or 'UNKNOWN',
            'title': event.get('title'),
            'reason': event.get('reason'),
            'epistemic': event.get('epistemic'),
            'priority': 'HIGH' if event.get('priority', 9) <= 1 else
                        'MEDIUM' if event.get('priority', 9) <= 3 else 'LOW',
            'period': event.get('period'),
            'evidence': event.get('evidence'),
            'action': event.get('action'),
            'ledger_revision': revision,
            'status': 'CONFIRMED' if action == 'confirm' else 'PROPOSED',
            'fingerprint': ident,
            'observed_at': current.isoformat(),
            'limitations': briefing.get('limitations') or [],
        })
    if state.get('revision') != revision:
        emit(store, 'ledger_revision', key='ledger', revision=revision)
    for insight in active:
        if insight.get('priority') == 'HIGH':
            emit(store, 'new_insight', key=insight['insight_id'], revision=revision)
    state.update({'revision': revision, 'insights': active,
                  'suppressed': suppressed, 'updated_at': current.isoformat()})
    _save(store, state)
    return {'insights': active, 'suppressed': suppressed, 'ledger_revision': revision,
            'coverage_complete': briefing.get('coverage_complete', False),
            'financial_apply': False}


def feedback(store: ReviewStore, insight_id: str, action: str, *, note: str = '',
             now: datetime | None = None, remind_minutes: int = 1440) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError('Unsupported feedback action')
    if not re.fullmatch(r'[a-f0-9]{8,64}', insight_id):
        raise ValueError('Invalid insight id')
    current = now or datetime.now(UTC)
    state = _load(store)
    record: dict[str, Any] = {'action': action, 'note': str(note)[:500],
                              'at': current.isoformat(), 'revision': state.get('revision')}
    if action == 'remind_later':
        record['until'] = (current + timedelta(minutes=max(1, remind_minutes))).isoformat()
    state.setdefault('feedback', {})[insight_id] = record
    _save(store, state)
    return {'status': 'saved', 'insight_id': insight_id, 'action': action,
            'financial_apply': False, 'owner_decision': True}


def retrieve(store: ReviewStore, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    state = _load(store)
    words = {word for word in re.findall(r'[a-zа-я0-9]{3,}', str(query).casefold())}
    scored = []
    for insight in state.get('insights') or []:
        if insight.get('status') not in {'PROPOSED', 'CONFIRMED'}:
            continue
        text = (str(insight.get('title')) + ' ' + str(insight.get('reason'))).casefold()
        score = sum(1 for word in words if word in text)
        if words and score == 0:
            continue
        scored.append((score, insight))
    scored.sort(key=lambda item: (-item[0], str(item[1].get('insight_id'))))
    return [insight for _, insight in scored[:max(1, min(limit, 10))]]


def memory_context(cfo: Any, store: ReviewStore, query: str, *, limit: int = 5) -> dict[str, Any]:
    memory = HumanLabels(store).memory()
    words = {word for word in re.findall(r'[a-zа-я0-9]{3,}', str(query).casefold())}

    def relevant(text: str) -> bool:
        return not words or any(word in text.casefold() for word in words)

    preferences = [note for note in memory.get('preferences', [])
                   if note.get('status') not in {'revoked', 'expired'} and relevant(str(note.get('text', '')))]
    facts = [fact for fact in memory.get('facts', []) if relevant(str(fact.get('category', '')))]
    goals = [goal for goal in cfo.goals(datetime.now(UTC).date())
             if relevant(str(goal.get('title', '')))]
    return {'preferences': preferences[:limit], 'facts': facts[:limit],
            'goals': goals[:limit], 'hypotheses_excluded': True,
            'revoked_excluded': True, 'financial_apply': False}
