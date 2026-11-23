"""Optional derived embedding index; ledger remains the only financial truth."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_MODEL = 'paraphrase-multilingual-MiniLM-L12-v2'


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a*b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a*a for a in left)) * math.sqrt(sum(b*b for b in right))
    return dot/norm if norm else 0.0


def rebuild_vector_index(db_path: Path, *, index_path: Path | None = None,
                         model_name: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Build a disposable exact-cosine index; no ledger writes."""
    from jarvis.rag.embedder import Embedder
    source = Path(db_path)
    target = Path(index_path or source.with_name(source.stem + '-vectors.sqlite'))
    with sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True) as conn:
        rows = conn.execute('SELECT id,merchant,description FROM transactions').fetchall()
    vectors = Embedder(model_name).encode(
        [f'{merchant or ""} {description or ""}'.strip() for _, merchant, description in rows])
    with sqlite3.connect(target) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS vector_index('
            'transaction_id INTEGER PRIMARY KEY, embedding TEXT NOT NULL, model TEXT NOT NULL)')
        conn.execute('DELETE FROM vector_index')
        conn.executemany('INSERT INTO vector_index VALUES(?,?,?)',
            [(ident, json.dumps(vector), model_name) for (ident, _, _), vector in zip(rows, vectors, strict=True)])
        conn.commit()
    return {'status': 'ready', 'index': str(target), 'documents': len(rows), 'model': model_name}


def vector_search(db_path: Path, text: str, *, limit: int = 20,
                  index_path: Path | None = None, model_name: str = DEFAULT_MODEL) -> list[dict[str, Any]]:
    """Return IDs and cosine scores only; callers re-read exact ledger rows."""
    from jarvis.rag.embedder import Embedder
    source = Path(db_path)
    target = Path(index_path or source.with_name(source.stem + '-vectors.sqlite'))
    if not target.is_file():
        rebuild_vector_index(source, index_path=target, model_name=model_name)
    query = Embedder(model_name).encode_one(text)
    with sqlite3.connect(f'file:{target.as_posix()}?mode=ro', uri=True) as conn:
        rows = conn.execute('SELECT transaction_id,embedding FROM vector_index WHERE model=?',
            (model_name,)).fetchall()
    ranked = sorted(((int(ident), _cosine(query, json.loads(vector))) for ident, vector in rows),
        key=lambda pair: pair[1], reverse=True)[:max(1, min(limit, 100))]
    return [{'id': ident, 'score': round(score, 6)} for ident, score in ranked if score > 0]
