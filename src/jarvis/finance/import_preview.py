"""Preview Russian statement imports and export owner-confirmed ML datasets."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.finance.bank.sber_statement import read_sber_export
from jarvis.finance.bank.statement_validation import validate_statement
from jarvis.finance.classification.evaluation import (
    Example,
    confirmed_examples,
    independent_split,
)
from jarvis.finance.classification.learning import normalized, provenance
from jarvis.finance.db import FinanceDB
from jarvis.finance.domain import Transaction


def _fingerprint(tx: Transaction) -> str:
    return hashlib.sha256(json.dumps([tx.tx_date.isoformat(), str(tx.amount), tx.currency,
        tx.merchant, tx.description], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def preview_import(db: FinanceDB, path: str | Path, *, account: str = 'sber') -> dict[str, Any]:
    """Parse one statement and classify rows without writing to the ledger."""
    parsed = read_sber_export(path, account=account)
    existing = db.all_transactions()
    by_external: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    weak_existing = set()
    for row in existing:
        if row.get('external_id'):
            by_external[(row['account'], row['external_id'])].append(row)
        merchant = normalized(str(row['merchant'] or ''))
        if merchant:
            weak_existing.add((row['tx_date'], Decimal(row['amount']), row['currency'], merchant))
    by_fingerprint = {_fingerprint(Transaction(
        tx_date=date.fromisoformat(str(row['tx_date'])),
        amount=Decimal(str(row['amount'])), currency=row['currency'],
        description=row['description'], merchant=row['merchant'], category=row['category'],
        account=row['account'], external_id=row.get('external_id'))): row for row in existing}
    occurrences = Counter(tx.external_id for tx in parsed.transactions if tx.external_id)
    source_identities: dict[str, set[tuple[str, Decimal, str]]] = defaultdict(set)
    for tx in parsed.transactions:
        if tx.external_id:
            source_identities[tx.external_id].add((tx.tx_date.isoformat(), tx.amount, tx.currency))
    source_conflicts = sum(len(values) > 1 for values in source_identities.values())
    assigned: Counter[str] = Counter()
    prepared: list[Transaction] = []
    for tx in parsed.transactions:
        identifier = tx.external_id
        if identifier and occurrences[identifier] > 1:
            assigned[identifier] += 1
            order = assigned[identifier]
            prepared.append(tx if order == 1 else replace(tx, external_id=f'{identifier}-{order}'))
        else:
            prepared.append(tx)
    new, duplicate, possible, possible_transactions = [], [], [], []
    conflicts = 0
    for tx in prepared:
        matches = by_external.get((tx.account, tx.external_id), []) if tx.external_id else []
        if matches:
            if all((row['tx_date'], Decimal(row['amount']), row['currency']) ==
                   (tx.tx_date.isoformat(), tx.amount, tx.currency) for row in matches):
                duplicate.append({'external_id': tx.external_id, 'date': tx.tx_date.isoformat()})
            else:
                conflicts += 1
        elif (_fingerprint(tx) in by_fingerprint
                or (tx.tx_date.isoformat(), tx.amount, tx.currency, normalized(tx.merchant)) in weak_existing):
            possible.append({'date': tx.tx_date.isoformat(), 'merchant': tx.merchant,
                'amount': str(tx.amount)})
            possible_transactions.append(tx)
        else:
            new.append(tx)
    validation = validate_statement(parsed.text, parsed.transactions,
                                    source_format=parsed.source_format)
    if conflicts or source_conflicts:
        validation['unsafe_reasons'].append('existing_identity_conflict' if conflicts else 'source_identity_conflict')
        validation.update(verdict='unsafe', can_import=False,
            notice='Банковский ID уже связан с другой датой, суммой или валютой. Импорт заблокирован до сверки источников.')
    return {'status': 'preview', 'format': parsed.source_format, 'account': account,
        'account_identity': {'status': 'UNKNOWN', 'declared_account': account,
            'document_suffix': validation.get('account_suffix'), 'reconciled': False},
        'identity_conflicts': conflicts,
        'source_identity_conflicts': source_conflicts,
        'new': len(new), 'duplicate': len(duplicate), 'possible_duplicate': len(possible),
        'warnings': parsed.warnings, 'new_transactions': new,
        'possible_transactions': possible_transactions,
        'duplicate_rows': duplicate, 'possible_duplicate_rows': possible,
        'validation': validation, 'can_import': validation['can_import'],
        'financial_apply': False, 'notice': 'Импорт не применён; требуется подтверждение владельца.'}


def apply_preview(db: FinanceDB, preview: dict[str, Any], *, owner_confirmed: bool = False,
                  include_possible_duplicates: bool = False,
                  acknowledge_validation: bool = False) -> dict[str, Any]:
    """Insert only previewed new rows after explicit owner confirmation."""
    if not owner_confirmed:
        return {'status': 'blocked', 'reason': 'owner_confirmation_required', 'inserted': 0}
    if preview.get('status') != 'preview':
        raise ValueError('A parsed preview is required')
    validation = preview.get('validation') or {}
    verdict = validation.get('verdict')
    if verdict == 'unsafe':
        return {'status': 'blocked', 'reason': 'validation_failed', 'inserted': 0,
                'validation': validation}
    if verdict == 'review' and not acknowledge_validation:
        return {'status': 'blocked', 'reason': 'validation_acknowledgement_required',
                'inserted': 0, 'validation': validation}
    if preview.get('possible_transactions') and not include_possible_duplicates:
        return {'status': 'blocked', 'reason': 'possible_duplicate_confirmation_required', 'inserted': 0}
    rows = list(preview.get('new_transactions') or [])
    if include_possible_duplicates:
        rows.extend(preview.get('possible_transactions') or [])
    inserted = db.add_transactions(rows)
    return {'status': 'applied', 'inserted': inserted, 'financial_apply': True}


_CARD = re.compile(r'\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b')
_PHONE = re.compile(r'(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}')
_LONG_DIGITS = re.compile(r'\d{6,}')


def _scrubbed(value: str) -> str:
    """Drop card, phone and account-like requisites before normalizing a label text."""
    text = _CARD.sub(' ', str(value or ''))
    text = _PHONE.sub(' ', text)
    return _LONG_DIGITS.sub(' ', text)


def export_training_dataset(cfo: Any, cutoff: Any) -> dict[str, Any]:
    """Export only owner-confirmed examples with leakage-safe splits; no AI labels."""
    dataset = provenance(cfo)
    eligible = {row['transaction_id'] for row in dataset['examples']
        if row['eligible'] and row['status'] == 'confirmed'}
    raw = {row['transaction_id']: row for row in dataset['examples'] if row['status'] == 'confirmed'}
    examples = []
    for row in confirmed_examples(cfo.db.path):
        if row.transaction_id not in eligible or row.label in {'unknown', 'pending_review', 'uncategorized'}:
            continue
        source = raw.get(row.transaction_id, {})
        examples.append(Example(row.transaction_id, row.day, normalized(_scrubbed(source.get('merchant') or row.merchant)),
            normalized(_scrubbed(source.get('text') or row.text)), row.label))
    train, test, split = independent_split(examples, cutoff)
    payload = {
        'version': 'owner-dataset-' + hashlib.sha256(json.dumps(
            [[e.transaction_id, e.label, e.text] for e in examples], sort_keys=True).encode()).hexdigest()[:16],
        'label_source': 'owner_confirmed_only', 'split': 'time_and_merchant_disjoint',
        'cutoff': str(cutoff), 'train': [{'transaction_id': e.transaction_id, 'day': e.day.isoformat(),
            'merchant': e.merchant, 'text': e.text, 'label': e.label} for e in train],
        'test': [{'transaction_id': e.transaction_id, 'day': e.day.isoformat(), 'merchant': e.merchant,
            'text': e.text, 'label': e.label} for e in test],
        'exclusions': split, 'ai_labels_used': False,
        'notice': 'Only owner-confirmed labels; test rows are never used for fitting.',
    }
    train_ids = {e.transaction_id for e in train}
    test_ids = {e.transaction_id for e in test}
    payload['leakage'] = bool(train_ids & test_ids)
    payload['train_ids'] = sorted(train_ids); payload['test_ids'] = sorted(test_ids)
    return payload
