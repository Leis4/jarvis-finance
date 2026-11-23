"""Small local learning and dashboard views over the existing CFO ledger."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jarvis.finance.db import FinanceDB, utc_now
from jarvis.finance.event_taxonomy import (
    NON_ECONOMIC_CATEGORIES,
    TAXONOMY_VERSION,
    canonical_category,
)
from jarvis.finance.ledger import cashflow
from jarvis.finance.personal_cfo import PersonalCFO, money
from jarvis.finance.personal_profile import business_date


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bank_enabled: bool = True
    sync_minutes: int = Field(default=30, ge=15, le=240)
    daily_enabled: bool = True
    daily_hour: int = Field(default=9, ge=8, le=22)
    weekly_enabled: bool = False
    weekly_hour: int = Field(default=9, ge=8, le=22)
    alerts_enabled: bool = True
    paid_classification_enabled: bool = True


def marketplace(merchant: str) -> bool:
    return bool(re.search(r'ozon|озон|wildberries|вайлдберриз|\bwb\b|\bвб\b|яндекс.?маркет|yandex.?market', merchant, re.IGNORECASE))


def similar(db: FinanceDB, tx_id: int, *, limit: int = 8) -> list[dict[str, Any]]:
    """Suggest nearby unconfirmed records locally; never apply a category."""
    rows = db.all_transactions()
    original = next((r for r in rows if r['id'] == tx_id), None)
    if not original or marketplace(original['merchant']):
        return []
    with db.connect() as conn:
        confirmed = {r[0] for r in conn.execute("SELECT DISTINCT transaction_id FROM category_corrections c WHERE source='manual' AND version=(SELECT MAX(c2.version) FROM category_corrections c2 WHERE c2.transaction_id=c.transaction_id)")}
    if tx_id not in confirmed:
        return []
    candidates = [r for r in rows[-2000:] if r['id'] != tx_id and r['id'] not in confirmed
        and r['currency'] == original['currency'] and (money(r['amount']) < 0) == (money(original['amount']) < 0)
        and r['category'] not in NON_ECONOMIC_CATEGORIES and not marketplace(r['merchant'])]
    if not candidates:
        return []
    from sklearn.feature_extraction.text import TfidfVectorizer
    def text(row: dict[str, Any]) -> str:
        return re.sub(r'\d{4,}', ' ', row['merchant'] + ' ' + row['description']).casefold()[:300]
    vectorizer = TfidfVectorizer(analyzer='char_wb', ngram_range=(3, 5), max_features=4000)
    try:
        matrix = vectorizer.fit_transform([text(original), *(text(r) for r in candidates)])
    except ValueError:
        return []
    scores = (matrix[1:] @ matrix[0].T).toarray().ravel()
    result = []
    for index in scores.argsort()[::-1][:limit]:
        candidate, score = candidates[index], float(scores[index])
        if score >= .7:
            result.append({'id': candidate['id'], 'merchant': candidate['merchant'], 'description': candidate['description'],
                'amount': candidate['amount'], 'category': candidate['category'], 'suggested_category': original['category'],
                'similarity': round(score, 3), 'similarity_is_probability': False, 'review': True})
    return result


def confirm_if_version(db: FinanceDB, tx_id: int, category: str, expected: int) -> bool:
    """Compare and append a manual correction atomically across web and Telegram."""
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT category FROM transactions WHERE id=?', (tx_id,)).fetchone()
        version = conn.execute('SELECT COALESCE(MAX(version),0) FROM category_corrections WHERE transaction_id=?', (tx_id,)).fetchone()[0]
        if row is None or version != expected:
            return False
        conn.execute('INSERT INTO category_corrections(transaction_id,version,old_category,new_category,source,created_at) VALUES(?,?,?,?,?,?)',
            (tx_id, version+1, row[0], category, 'manual', utc_now()))
        conn.execute('UPDATE transactions SET category=? WHERE id=?', (category, tx_id))
        db.audit(conn, 'category_updated', {'tx_id': tx_id, 'source': 'manual', 'version': version+1, 'taxonomy_version': TAXONOMY_VERSION})
        return True


def validated_category(category: str) -> str:
    category=canonical_category(category.strip())
    if not re.fullmatch(r'[a-z_][a-z0-9_:]{0,79}',category) or category in {'unknown','pending_review','uncategorized'}:
        raise ValueError('Выберите экономическую категорию.')
    return category


def review_batch(cfo: PersonalCFO, group_id: str, items: list[dict[str, Any]], category: str | None) -> dict[str, Any]:
    """Validate every selected row, then apply one atomic owner confirmation or undo."""
    from jarvis.finance.classification.learning import fingerprint, review_group_id
    if not 1<=len(items)<=100 or len({item['id'] for item in items})!=len(items):
        raise ValueError('Выберите от 1 до 100 различных операций.')
    if category is not None:
        category=validated_category(category)
    changes: list[dict[str, Any]]=[]
    with cfo.db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for item in items:
            raw=conn.execute('SELECT * FROM transactions WHERE id=?',(item['id'],)).fetchone()
            if raw is None:
                raise ValueError('Операция удалена. Обновите очередь.')
            latest=conn.execute('SELECT * FROM category_corrections WHERE transaction_id=? ORDER BY version DESC LIMIT 1',(item['id'],)).fetchone()
            row=dict(raw)
            version=int(latest['version']) if latest else 0
            digest=fingerprint([row[k] for k in ('merchant','description','amount','currency')])
            if review_group_id(row)!=group_id or version!=item['expected_version'] or digest!=item['input_sha256']:
                raise ValueError('Группа или операция изменилась. Ничего не применено; обновите очередь.')
            if category is None and (not latest or latest['source']!='manual' or latest['new_category']!=row['category']):
                raise ValueError('Подтверждение уже отозвано или изменено.')
            changes.append({'id':row['id'],'expected_version':version+1,'input_sha256':digest,
                'old':row['category'],'new':category if category is not None else latest['old_category']})
        source='manual' if category is not None else 'manual_undo'
        for change in changes:
            conn.execute('INSERT INTO category_corrections(transaction_id,version,old_category,new_category,source,created_at) VALUES(?,?,?,?,?,?)',
                (change['id'],change['expected_version'],change['old'],change['new'],source,utc_now()))
            conn.execute('UPDATE transactions SET category=? WHERE id=?',(change['new'],change['id']))
            cfo.db.audit(conn,'category_updated' if category is not None else 'category_undo',
                {'tx_id':change['id'],'source':source,'version':change['expected_version'],'taxonomy_version':TAXONOMY_VERSION,'owner_group_action':True})
        cfo.db.audit(conn,'cfo_group_review',{'group_id':group_id,'source':source,'count':len(changes)})
    return {'status':'saved','group_id':group_id,'count':len(changes),'items':[{k:change[k] for k in ('id','expected_version','input_sha256')} for change in changes]}


def undo_correction(db: FinanceDB, tx_id: int, expected: int | None = None) -> bool:
    """Append an undo only for the current manual version; preserve all money fields."""
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        latest = conn.execute('SELECT * FROM category_corrections WHERE transaction_id=? ORDER BY version DESC LIMIT 1', (tx_id,)).fetchone()
        row = conn.execute('SELECT category FROM transactions WHERE id=?', (tx_id,)).fetchone()
        if (not latest or not row or latest['source'] != 'manual' or row[0] != latest['new_category']
                or expected is not None and latest['version'] != expected):
            return False
        conn.execute('INSERT INTO category_corrections(transaction_id,version,old_category,new_category,source,created_at) VALUES(?,?,?,?,?,?)',
            (tx_id, latest['version']+1, row[0], latest['old_category'], 'manual_undo', utc_now()))
        conn.execute('UPDATE transactions SET category=? WHERE id=?', (latest['old_category'], tx_id))
        db.audit(conn, 'category_undo', {'tx_id': tx_id, 'version': latest['version']+1})
        return True


def overview(cfo: PersonalCFO, *, now: datetime | None = None, days: int = 30) -> dict[str, Any]:
    """Calculate monthly charts/budgets with Decimal, labeling incomplete evidence."""
    now = now or datetime.now(UTC)
    days = 30 if days not in {7, 30, 90} else days
    today = business_date(now)
    window = max(29, days - 1)
    rows = cfo.economic_rows(cfo.db.transactions_between(min(today.replace(day=1), today-timedelta(days=window)), today))
    flow = cashflow([r for r in rows if r['tx_date'] >= today.replace(day=1).isoformat()])
    limits = cfo.db.get_setting('cfo.category_limits', {})
    daily = []
    for offset in range(window, -1, -1):
        day = today-timedelta(days=offset)
        selected = [r for r in rows if r['tx_date'] == day.isoformat()]
        item = cashflow(selected)
        daily.append({'date': day.isoformat(), 'income': str(item['income']), 'expenses': str(item['expenses'])})
    with cfo.db.connect() as conn:
        confirmed = [r[0] for r in conn.execute("SELECT DISTINCT transaction_id FROM category_corrections c JOIN transactions t ON t.id=c.transaction_id WHERE c.source='manual' AND c.new_category=t.category AND c.version=(SELECT MAX(c2.version) FROM category_corrections c2 WHERE c2.transaction_id=c.transaction_id)")]
        versions = {str(r[0]): r[1] for r in conn.execute('SELECT transaction_id,MAX(version) FROM category_corrections GROUP BY transaction_id')}
    from jarvis.finance.personal_profile import financial_decision
    position=financial_decision(cfo.bank_snapshot(), cfo.profile(), rows, cfo.goals(today), now=now)
    return {'month': today.strftime('%Y-%m'), 'days': days,
        'flow': {k: str(flow[k]) for k in ('income', 'expenses', 'movement')},
        'free_to_spend': position.get('headroom'), 'free_notes': position.get('missing'),
        'reserve_effective': position.get('reserve_effective'),
        'categories': [{'category': k, 'spent': str(v), 'limit': limits.get(k),
            'remaining': str(money(limits[k])-v) if k in limits else None} for k, v in sorted(flow['by_category'].items(), key=lambda pair: pair[1], reverse=True)],
        'daily': daily, 'limits': limits, 'confirmed_samples': len(confirmed), 'confirmed_ids': confirmed, 'versions': versions,
        'learning': 'Confirmed local examples; TF-IDF candidates only, no automatic model promotion',
        'coverage_complete': False, 'observed_at': now.isoformat()}
