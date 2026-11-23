"""Derived FTS5/hybrid transaction search; source ledger remains read-only truth."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

_SAFE_TOKEN = re.compile(r'[0-9A-Za-zА-Яа-яЁё]+')


def default_index_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.stem + '-search.sqlite')


def fts_query(text: str) -> str:
    """Quote user tokens for FTS5; never pass raw operators from owner text."""
    tokens = _SAFE_TOKEN.findall(text.casefold())[:20]
    return ' AND '.join('"'+token.replace('"', '')+'"' for token in tokens)


def rebuild_index(db_path: Path, index_path: Path | None = None) -> dict[str, Any]:
    """Build a disposable FTS index from the financial ledger."""
    source = Path(db_path)
    target = Path(index_path or default_index_path(source))
    if not source.is_file():
        raise FileNotFoundError('Financial database not found')
    if target.resolve() == source.resolve() or (target.exists() and target.samefile(source)):
        raise ValueError('Search index must be separate from the financial database')
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True) as src:
        rows = src.execute('SELECT id,merchant,description,category,tx_date,amount,currency FROM transactions').fetchall()
    with sqlite3.connect(target) as conn:
        conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS transaction_fts USING fts5('
            'merchant, description, category, transaction_id UNINDEXED, tx_date UNINDEXED, '
            "tokenize='unicode61 remove_diacritics 2')")
        conn.execute('DELETE FROM transaction_fts')
        conn.executemany('INSERT INTO transaction_fts(merchant,description,category,transaction_id,tx_date) '
            'VALUES(?,?,?,?,?)', [(merchant or '', description or '', category or '', str(ident), str(day))
                for ident, merchant, description, category, day, _, _ in rows])
        conn.commit()
    return {'status': 'ready', 'index': str(target), 'documents': len(rows)}


def search_transactions(db_path: Path, text: str, *, limit: int = 20,
                        index_path: Path | None = None) -> list[dict[str, Any]]:
    """Exact FTS5/BM25 search; returns ledger-provenance rows, not model memory."""
    query = fts_query(text)
    if not query:
        return []
    source = Path(db_path)
    target = Path(index_path or default_index_path(source))
    # shortcut: rebuild the small local index per query; add revision tracking if measured latency warrants it.
    rebuild_index(source, target)
    with sqlite3.connect(target.resolve().as_uri()+'?mode=ro', uri=True) as conn:
        found = conn.execute('SELECT transaction_id, bm25(transaction_fts) AS rank '
            'FROM transaction_fts WHERE transaction_fts MATCH ? ORDER BY rank LIMIT ?',
            (query, max(1, min(limit, 100)))).fetchall()
    ids = [int(row[0]) for row in found]
    if not ids:
        return []
    placeholders = ','.join('?'*len(ids))
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = {row['id']: dict(row) for row in conn.execute(
            f'SELECT id,merchant,description,category,tx_date,amount,currency FROM transactions WHERE id IN ({placeholders})', ids)}
    return [{**rows[int(ident)], 'rank': float(rank)} for ident, rank in found if int(ident) in rows]


def rrf(rankings: list[list[dict[str, Any]]], *, key: str = 'id', k: int = 60) -> list[dict[str, Any]]:
    """Reciprocal rank fusion for independent lexical/vector result lists."""
    score: dict[Any, float] = {}
    rows: dict[Any, dict[str, Any]] = {}
    for ranking in rankings:
        for position, row in enumerate(ranking, 1):
            ident = row.get(key)
            if ident is None:
                continue
            score[ident] = score.get(ident, 0.0) + 1.0/(k+position)
            rows.setdefault(ident, row)
    return [{**rows[ident], 'rank': round(value, 6)}
        for ident, value in sorted(score.items(), key=lambda pair: pair[1], reverse=True)]


def hybrid_search(db_path: Path, text: str, *, limit: int = 20,
                  vector_results: list[dict[str, Any]] | None = None,
                  index_path: Path | None = None) -> list[dict[str, Any]]:
    """FTS5 plus optional vector candidates; SQL rows remain authoritative."""
    lexical = search_transactions(db_path, text, limit=limit, index_path=index_path)
    if not vector_results:
        return lexical
    ids = list(dict.fromkeys(row['id'] for row in vector_results[:100]
        if type(row.get('id')) is int and row['id'] > 0))
    if not ids:
        return lexical
    placeholders = ','.join('?'*len(ids))
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        canonical = {row['id']: dict(row) for row in conn.execute(
            f'SELECT id,merchant,description,category,tx_date,amount,currency FROM transactions WHERE id IN ({placeholders})', ids)}
    # Retrieval provides relevance only; money and provenance always come from the ledger.
    vectors = [canonical[ident] for ident in ids if ident in canonical]
    return rrf([lexical, vectors])[:max(1, min(limit, 100))]
