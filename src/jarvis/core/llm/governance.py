"""One-use provider dispatch permits bound to the canonical CFO reservation."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from jarvis.core.llm.budget_store import BudgetStore
from jarvis.core.llm.errors import LLMUnavailable


def canonical_journal() -> Path:
    from jarvis.core.config import settings
    return Path(settings.db_path).resolve().parent / "cfo-ai" / "personal-llm.sqlite"


@dataclass
class Dispatch:
    journal: Path
    request_id: str
    model: str
    prompt_sha: str
    output_cap: int
    consumed: bool = False


_DISPATCH: ContextVar[Dispatch | None] = ContextVar("cfo_paid_dispatch", default=None)


@contextmanager
def paid_dispatch(store: BudgetStore, request_id: str, model: str, prompt: str, output_cap: int) -> Iterator[None]:
    value = Dispatch(store.path.resolve(), request_id, model, hashlib.sha256(prompt.encode()).hexdigest(), output_cap)
    token = _DISPATCH.set(value)
    try:
        yield
    finally:
        _DISPATCH.reset(token)


def consume_dispatch(model: str, prompt: str, output_cap: int) -> bool:
    """Deny any live cloud call without a matching attempted global reservation."""
    value = _DISPATCH.get()
    if (value is None or value.consumed or value.journal != canonical_journal()
            or value.model != model or value.output_cap != output_cap
            or value.prompt_sha != hashlib.sha256(prompt.encode()).hexdigest()):
        raise LLMUnavailable("Paid route disabled: canonical global reservation required")
    store = BudgetStore(value.journal, currency="RUB")
    state = store.request_status(value.request_id)
    if not state or state['status'] != 'attempted':
        raise LLMUnavailable("Attempted global reservation required")
    with store._transaction() as conn:
        if conn.execute("SELECT version FROM cfo_ai_policy").fetchone() != (1,):
            raise LLMUnavailable("Global budget policy missing")
    value.consumed = True
    return True
