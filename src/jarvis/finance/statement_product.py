"""Statement acceptance and reproducible projections through the existing Ledger."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.finance.audit_report import AcceptedStatementScope

from jarvis.finance.bank import sber_statement
from jarvis.finance.classification.taxonomy import OUTGOING_CLASSES
from jarvis.finance.db import FinanceDB
from jarvis.finance.domain import Transaction
from jarvis.finance.ledger import cashflow, finite_money, reconcile, transaction_payload
from jarvis.finance.production_intake import build_statement_preview
from jarvis.finance.review_backup import review_snapshot
from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_store import ReviewStore, _item_key, encoded
from jarvis.finance.statement_review import validate_statement_intake


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def parser_version() -> str:
    return 'sber-pdf:' + hashlib.sha256(Path(sber_statement.__file__).read_bytes()).hexdigest()


def economic_category(row: dict[str, Any], label: dict[str, Any] | None) -> str:
    """Share the existing projection's compatible human-category contract."""
    category = label['category'] if label and label['action'] == 'confirm' else 'pending_review'
    outgoing = {a for k,v in OUTGOING_CLASSES.items() if k not in {'unknown','transfer_out','investment','savings','cash'}
                for a in [k,*v['aliases']]}
    allowed = ({'salary','stipend','scholarship','family_income','family_support','parental_support','gift','bank_interest'}
               if finite_money(row['amount']) > 0 else outgoing | {'bank_fees','loan_interest','interest_expense'})
    return category if category in allowed else 'pending_review'


class StatementProduct:
    """Reuse private review evidence, labels and FinanceDB; never open legacy for writing."""

    def __init__(self, store: ReviewStore):
        self.store = store

    def _folder(self, name: str) -> Path:
        folder = self.store.workspace / name
        if folder.is_symlink() or any(p.is_symlink() for p in (self.store.workspace, *self.store.workspace.parents)):
            raise ValueError('Unsafe private statement workspace')
        return folder

    def _source(self, key: str, store: ReviewStore | None = None) -> dict[str, Any]:
        if not re.fullmatch(r'[0-9a-f]{64}', key):
            raise ValueError('Invalid statement identity')
        source = json.loads((store or self.store).metadata()['statement:' + key])
        if digest(source) != key or source['parser_version'] != parser_version():
            raise ValueError('Statement or parser changed; prepare a new preview')
        if source['format'] not in {'pdf', 'csv'} or source['original'] != source['sha256'] + '.' + source['format']:
            raise ValueError('Invalid original identity')
        path = self._folder('statement-originals') / source['original']
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('Statement original unavailable or changed')
        return dict(source)

    def prepare(self, data: bytes, filename: str, *, csv_scope: dict[str, Any] | None = None) -> dict[str, Any]:
        """Quarantine an original once and reuse the PDF/CSV parser without accepting it."""
        self.store.audit()
        suffix = Path(filename).suffix.lower()
        if (Path(filename).name != filename or '/' in filename or '\\' in filename or suffix not in {'.pdf', '.csv'}
                or not 0 < len(data) <= 20 * 1024 * 1024 or (suffix == '.pdf' and not data.startswith(b'%PDF-'))):
            raise ValueError('Expected a bounded original PDF/CSV document')
        folder = self._folder('statement-originals')
        folder.mkdir(exist_ok=True)
        sha = hashlib.sha256(data).hexdigest()
        original = sha + suffix
        path = folder / original
        if path.is_symlink():
            raise ValueError('Unsafe original')
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError('Original identity conflict')
        else:
            with path.open('xb') as output:
                output.write(data)
        mapping = {i['bank_account']: i['local_account'] for i in self.store.items()
                   if i.get('bank_account') and i.get('local_account')}
        if suffix == '.pdf':
            preview = build_statement_preview(path, mapping)
        else:
            if csv_scope is None:
                raise ValueError('CSV needs an original declared account/period/balance scope; use PDF for automatic metadata')
            scope = dict(csv_scope)
            check = validate_statement_intake(path, scope)
            if not check['valid']:
                raise ValueError('CSV scope evidence incomplete')
            parsed = sber_statement.read_sber_export(path, account=scope['account'])
            ops = [transaction_payload(t) for t in parsed.transactions]
            incoming = sum((finite_money(t['amount']) for t in ops if finite_money(t['amount']) > 0), Decimal(0))
            outgoing = sum((-finite_money(t['amount']) for t in ops if finite_money(t['amount']) < 0), Decimal(0))
            preview = {'status': 'checks_passed_read_only', 'metadata': {
                'bank_account': scope['account'], 'local_account': scope['account'], 'mapping_status': 'mapped',
                'currency': scope['currency'], 'period': [scope['start'], scope['end']],
                'opening': str(finite_money(scope['opening'])), 'closing': str(finite_money(scope['closing']))},
                'transactions': ops, 'issues': [], 'parser': {'warnings': parsed.warnings},
                'reconciliation': {'inflow': str(incoming), 'outflow': str(outgoing), 'operation_count': len(ops)}}
            if len(ops) != scope['operation_count'] or incoming - outgoing != finite_money(scope['movement']):
                raise ValueError('CSV declared operation count/movement mismatch')
        meta = preview['metadata']
        if (preview['status'] != 'checks_passed_read_only' or (suffix == '.csv' and preview['parser'].get('warnings'))
                or not meta.get('local_account') or not meta.get('period')):
            raise ValueError('Statement checks or explicit account mapping need review')
        start, end = map(date.fromisoformat, meta['period'])
        if end < start:
            raise ValueError('Invalid statement period')
        rows = preview['transactions']
        if not rows or any(t['currency'] != meta['currency'] or not start.isoformat() <= t['tx_date'] <= end.isoformat() for t in rows):
            raise ValueError('Operations outside declared currency/period')
        balance_check = reconcile([dict(t, id=n, account=meta['local_account']) for n, t in enumerate(rows)],
            meta['local_account'], meta['currency'], start, end, finite_money(meta['opening']), finite_money(meta['closing']))
        if balance_check.status != 'reconciled':
            raise ValueError('Statement balance invariant mismatch')
        source = {'sha256': sha, 'original': original, 'format': suffix[1:], 'parser_version': parser_version(),
            'metadata': meta, 'transactions': rows, 'reconciliation': preview['reconciliation'],
            'parser_warnings': preview['parser'].get('warnings', [])}
        key = digest(source)
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.store._audit(conn)
            conn.execute('INSERT OR IGNORE INTO review_meta VALUES (?,?)', ('statement:' + key, encoded(source)))
            conn.commit()
        current = next(s for s in self.sources() if s['id'] == key)
        return {'id': key, 'source': source, 'accepted': current['accepted'], 'revision': current['revision'], 'private_review_modified': True,
                'production_database_modified': False, 'financial_apply_allowed': False}

    def sources(self, store: ReviewStore | None = None) -> list[dict[str, Any]]:
        frozen = store or self.store
        latest = {e['item_id']: e for e in frozen.audit() if e.get('domain') == 'statement_source'}
        result = []
        for key in frozen.metadata():
            if key.startswith('statement:'):
                source = self._source(key[10:], frozen)
                event = latest.get(key)
                result.append({'id': key[10:], **source, 'revision': event['seq'] if event else 0,
                    'accepted': bool(event and event['action'] == 'accept_statement')})
        return result

    def decide(self, key: str, *, actor: str, expected_revision: int, accept: bool,
               acknowledge_parser_warnings: bool = False) -> dict[str, Any]:
        """Owner source acceptance is separate from categories and legacy identity decisions."""
        if not actor.strip():
            raise ValueError('Explicit owner identity required')
        source = self._source(key)
        if accept and source['parser_warnings'] and not acknowledge_parser_warnings:
            raise ValueError('Parser warnings need explicit source review, not automatic acceptance')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            events = self.store._audit(conn)
            old = next((e for e in reversed(events) if e.get('domain') == 'statement_source' and e['item_id'] == 'statement:' + key), None)
            if (old['seq'] if old else 0) != expected_revision:
                raise ValueError('Stale source decision; refresh preview')
            if not accept and old is None:
                raise ValueError('Source not previously accepted')
            if old and (old['action'] == 'accept_statement') == accept:
                return {**old, 'production_database_modified': False, 'legacy_merge_allowed': False}
            if accept:
                others = [s for s in self.sources() if s['accepted'] and s['id'] != key]
                if any(s['sha256'] == source['sha256'] for s in others):
                    raise ValueError('Original already accepted under another scope; no duplicate source mapping')
                meta = source['metadata']
                if any(s['metadata']['local_account'] == meta['local_account'] and s['metadata']['currency'] == meta['currency']
                       and s['metadata']['period'][0] <= meta['period'][1] and s['metadata']['period'][1] >= meta['period'][0] for s in others):
                    raise ValueError('Overlapping statement versions need manual review; no automatic merge')
                packet = json.loads(self.store.metadata()['packet'])
                if packet.get('source_kind', 'pdf') != 'pdf':
                    raise ValueError('Use an existing PDF review workspace')
                retained = {_item_key(i): i for i in packet['items']}
                for n, op in enumerate(source['transactions']):
                    item = {'source_sha256': source['sha256'], 'pdf_index': n, 'operation': op,
                        'bank_account': source['metadata']['bank_account'], 'local_account': source['metadata']['local_account'],
                        'classification': 'uncompared_statement', 'candidates': [], 'card': source['metadata'].get('card')}
                    ident = _item_key(item)
                    if ident in retained:
                        fields = ('tx_date', 'amount', 'currency', 'merchant', 'description')
                        if any(str(retained[ident]['operation'].get(f)) != str(op.get(f)) for f in fields):
                            raise ValueError('Existing review evidence differs; keep pending review')
                    else:
                        retained[ident] = item
                        conn.execute('INSERT INTO review_items VALUES (?,?)', (ident, encoded(item)))
                packet['items'] = list(retained.values())
                packet['classification_counts'] = dict(Counter(i['classification'] for i in retained.values()))
                content = encoded(packet)
                conn.execute('UPDATE review_meta SET value=? WHERE key=?', (content, 'packet'))
                conn.execute('UPDATE review_meta SET value=? WHERE key=?', (hashlib.sha256(content.encode()).hexdigest(), 'packet_hash'))
            event = {'seq': len(events) + 1, 'domain': 'statement_source', 'item_id': 'statement:' + key,
                'action': 'accept_statement' if accept else 'revoke_statement', 'actor': actor.strip(),
                'source_sha256': source['sha256'], 'timestamp': datetime.now(UTC).isoformat(),
                'parser_warnings_acknowledged': acknowledge_parser_warnings,
                'supersedes': old['seq'] if old else None}
            self.store._append_event(conn, event)
            conn.commit()
        return {**event, 'production_database_modified': False, 'legacy_merge_allowed': False}

    def _binding(self, store: ReviewStore) -> str:
        implementation = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        return digest([[(s['id'], s['revision'], s['accepted']) for s in self.sources(store)],
                       HumanLabels(store).audit(), parser_version(), implementation, OUTGOING_CLASSES])

    def history(self, *, asof: date | None = None) -> dict[str, Any]:
        """Explain accepted historical observations independently of today's money."""
        from jarvis.finance.statement_diagnostics import bound_warning_evidence

        checked = asof or datetime.now(UTC).date()
        with review_snapshot(self.store) as frozen:
            all_sources = self.sources(frozen)
            accepted = [s for s in all_sources if s['accepted']]
            if not accepted:
                raise ValueError('Accepted sources required for historical analysis')
            labels = HumanLabels(frozen).latest()
            scopes = []
            monthly: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for source in accepted:
                meta, rows = source['metadata'], source['transactions']
                opening, closing = finite_money(meta['opening']), finite_money(meta['closing'])
                credit = sum((finite_money(r['amount']) for r in rows if finite_money(r['amount']) > 0), Decimal(0))
                debit = sum((-finite_money(r['amount']) for r in rows if finite_money(r['amount']) < 0), Decimal(0))
                if (closing - opening != credit - debit or not rows or any(r['currency'] != meta['currency']
                        or not meta['period'][0] <= r['tx_date'] <= meta['period'][1] for r in rows)):
                    raise ValueError('Historical source scope mismatch')
                end = date.fromisoformat(meta['period'][1])
                scopes.append({'source_id': source['id'], 'sha256': source['sha256'], 'revision': source['revision'],
                    'parser_version': source['parser_version'], 'account_alias': '••' + str(meta['local_account'])[-4:],
                    'currency': meta['currency'], 'period': meta['period'], 'observations': len(rows),
                    'opening': str(opening), 'opening_at': meta['period'][0], 'closing': str(closing), 'closing_at': str(end),
                    'credits': str(credit), 'debits': str(debit), 'signed_movement': str(credit - debit), 'discrepancy': '0.00',
                    'current_balance_status': 'available_observation_only' if 0 <= (checked-end).days <= 1 else 'stale',
                    'warning_evidence': bound_warning_evidence(self, source),
                    'transfer_candidates': sum(any(w in str(r['description']).casefold() for w in ('перевод', 'transfer')) for r in rows),
                    'refund_candidates': sum(finite_money(r['amount']) > 0 and any(w in str(r['description']).casefold()
                        for w in ('возврат', 'refund')) for r in rows)})
                for row in rows:
                    monthly.setdefault((row['tx_date'][:7], row['currency']), []).append(row)
            # Calendar coverage is measured across ALL known accounts, including revoked/pending scopes.
            start = date.fromisoformat(min(s['metadata']['period'][0] for s in accepted)).replace(day=1)
            stop = date.fromisoformat(max(s['metadata']['period'][1] for s in accepted))
            periods = []
            while start <= stop:
                following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
                end = following - timedelta(days=1)
                for currency in sorted({s['metadata']['currency'] for s in all_sources}):
                    accounts = sorted({s['metadata']['local_account'] for s in all_sources if s['metadata']['currency'] == currency})
                    coverage = []
                    for account in accounts:
                        intervals = sorted((max(str(start), s['metadata']['period'][0]), min(str(end), s['metadata']['period'][1]))
                            for s in accepted if s['metadata']['local_account'] == account and s['metadata']['currency'] == currency
                            and s['metadata']['period'][0] <= str(end) and s['metadata']['period'][1] >= str(start))
                        cursor = start
                        for left, right in intervals:
                            if date.fromisoformat(left) > cursor:
                                break
                            cursor = max(cursor, date.fromisoformat(right) + timedelta(days=1))
                        coverage.append({'account_alias': '••' + str(account)[-4:], 'intervals': intervals,
                                         'full_calendar_month': cursor > end})
                    rows = monthly.get((str(start)[:7], currency), [])
                    credits = sum((finite_money(r['amount']) for r in rows if finite_money(r['amount']) > 0), Decimal(0))
                    debits = sum((-finite_money(r['amount']) for r in rows if finite_money(r['amount']) < 0), Decimal(0))
                    observed = any(c['intervals'] for c in coverage)
                    periods.append({'month': str(start)[:7], 'currency': currency, 'observations': len(rows),
                        'observed_credits': str(credits) if observed else None, 'observed_debits': str(debits) if observed else None,
                        'signed_movement': str(credits-debits) if observed else None, 'account_coverage': coverage,
                        'full_calendar_month': all(c['full_calendar_month'] for c in coverage), 'subtotal_only': True})
                start = following
            accepted_ids = {_item_key({'source_sha256': s['sha256'], 'pdf_index': n})
                            for s in accepted for n in range(len(s['transactions']))}
            common = [max(s['metadata']['period'][0] for s in accepted), min(s['metadata']['period'][1] for s in accepted)]
            current_status = 'stale' if any(s['current_balance_status'] == 'stale' for s in scopes) else 'insufficient_data'
            return {'status': 'available', 'scope': 'HISTORICAL_ACCEPTED_PDF_FACTS' if all(s['format'] == 'pdf' for s in accepted)
                    else 'HISTORICAL_ACCEPTED_STATEMENT_FACTS', 'checked_asof': str(checked),
                'binding': self._binding(frozen), 'source_scopes': scopes, 'monthly': periods,
                'source_periods_differ': len({tuple(s['period']) for s in scopes}) > 1,
                'common_source_window': common if common[0] <= common[1] else None,
                'accepted_sources': len(scopes), 'observations': len(accepted_ids),
                'human_confirmed_categories': sum(i in accepted_ids and l['action'] == 'confirm' for i,l in labels.items()),
                'unknown_economic_type': sum(economic_category(row, labels.get(_item_key({'source_sha256': s['sha256'], 'pdf_index': n})))
                    == 'pending_review' for s in accepted for n,row in enumerate(s['transactions'])),
                'confirmed_income': None, 'confirmed_expense': None, 'economic_status': 'requires_economic_report',
                'observation_types': {
                    'BANK_CREDIT_OBSERVATION': sum(finite_money(r['amount']) > 0 for s in accepted for r in s['transactions']),
                    'BANK_DEBIT_OBSERVATION': sum(finite_money(r['amount']) < 0 for s in accepted for r in s['transactions']),
                    'INTERNAL_TRANSFER_CANDIDATE': sum(s['transfer_candidates'] for s in scopes),
                    'transfer_candidates_are_confirmed': False},
                'current_position': {'scope': 'CURRENT_VERIFIED_FINANCIAL_POSITION', 'status': current_status,
                    'balance': None, 'available_cash': None, 'total_assets': None, 'liabilities': None, 'investment_capacity': 'BLOCKED'},
                'legacy_rehearsal_required_for_history': False, 'coverage_complete': False,
                'database_modified': False, 'financial_apply_allowed': False,
                'notice': 'Пополнения/списания — банковские наблюдения, не доходы/расходы. Исторический closing не является сегодняшними деньгами.'}

    def category_review(self, *, limit: int = 20) -> dict[str, Any]:
        """Prioritize identical local economic contexts; proposals never create labels."""
        from jarvis.finance.classification.suggestions import (
            LocalSuggestions,
            rules_revision,
        )

        if not 1 <= limit <= 20:
            raise ValueError('Bounded category shortlist required')
        with review_snapshot(self.store) as frozen:
            sources = [s for s in self.sources(frozen) if s['accepted']]
            source_items = {_item_key({'source_sha256': s['sha256'], 'pdf_index': n}): (s, n, row)
                            for s in sources for n, row in enumerate(s['transactions'])}
            latest = HumanLabels(frozen).latest()
            items = frozen.items()
            cohorts: dict[str, list[dict[str, Any]]] = {}
            for item in items:
                if item['id'] not in source_items or latest.get(item['id'], {}).get('action') in {'confirm','reject'}:
                    continue
                source, n, row = source_items[item['id']]
                if any(str(item['operation'].get(f)) != str(row.get(f)) for f in ('tx_date','amount','currency','merchant','description')):
                    raise ValueError('Category review original payload differs')
                amount = finite_money(row['amount'])
                # Exact normalized text, not amount/date proximity or guessed merchant aliases.
                identity = [' '.join(str(row.get(f) or '').casefold().split()) for f in ('merchant','description')]
                signature = digest([identity, source['metadata']['local_account'], row['currency'], amount < 0,
                                    bool(item.get('conflict_reasons')), item['status'] == 'reject'])
                cohorts.setdefault(signature, []).append({'item_id': item['id'], 'source_id': source['id'],
                    'source_sha256': source['sha256'], 'source_revision': source['revision'], 'ordinal': n,
                    'operation_hash': digest(row), 'label_revision': latest.get(item['id'], {}).get('seq', 0),
                    'date': row['tx_date'], 'amount': str(amount), 'currency': row['currency'],
                    'account_alias': '••' + str(source['metadata']['local_account'])[-4:],
                    'excluded': bool(item.get('conflict_reasons')) or item['status'] == 'reject'})
            suggestions = LocalSuggestions(frozen)
            suggestion_context = suggestions._context(frozen)
            binding = self._binding(frozen)
            groups = []
            policy = HumanLabels(frozen).policy()
            for signature, members in cohorts.items():
                proposal = suggestions._preview_frozen(frozen, members[0]['item_id'], _context=suggestion_context)
                category = proposal.suggested_category
                # Every member has the same full classification input and guard flags.
                if any(m['excluded'] for m in members) or proposal.reason_code == 'conflicting_human_feedback':
                    category = None
                text = next(r for i, (_, _, r) in source_items.items() if i == members[0]['item_id'])['description'].casefold()
                hypothesis = ('transfer_candidate' if any(w in text for w in ('перевод','transfer')) else
                              'refund_candidate' if finite_money(members[0]['amount']) > 0 and any(w in text for w in ('возврат','refund')) else 'unknown_economic_type')
                dates = sorted({m['date'] for m in members})
                amounts: dict[str, set[str]] = {}
                for m in members:
                    amounts.setdefault(m['amount'], set()).add(m['date'])
                recurring = any(len(ds) >= 3 and (date.fromisoformat(max(ds))-date.fromisoformat(min(ds))).days >= 28
                                for ds in amounts.values())
                eligible = bool(category and policy and category in policy['categories'] and len(members) <= 200)
                proof = digest([binding, signature, members, rules_revision()])
                groups.append({'group_hash': proof, 'candidate_category': category, 'reason': proposal.reason_code,
                    'merchant': str(source_items[members[0]['item_id']][2].get('merchant') or '')[:80],
                    'count': len(members), 'members': members, 'examples': members[:3], 'dates': [dates[0], dates[-1]],
                    'hypothesis': hypothesis, 'recurring_candidate': recurring, 'recurrence_confirmed': False,
                    'eligible_one_owner_decision': eligible, 'exceptions': sum(m['excluded'] for m in members),
                    'source_binding': binding, 'human_confirmed': False, 'apply_allowed': False})
            groups.sort(key=lambda g: (not g['eligible_one_owner_decision'], g['reason'] != 'human_feedback', -g['count'], g['group_hash']))
            return {'status': 'available' if sources else 'unavailable', 'scope': 'accepted_source_category_proposals',
                'binding': binding, 'groups': groups[:limit], 'total_groups': len(groups), 'shortlist_limit': limit,
                'proposal_groups': sum(g['candidate_category'] is not None for g in groups),
                'proposal_item_ids': sum(g['count'] for g in groups if g['candidate_category'] is not None),
                'eligible_owner_groups': sum(g['eligible_one_owner_decision'] for g in groups),
                'eligible_multi_item_groups': sum(g['eligible_one_owner_decision'] and g['count'] > 1 for g in groups),
                'shortlist_proposal_items': sum(g['count'] for g in groups[:limit] if g['candidate_category'] is not None),
                'unproposed_items': sum(g['count'] for g in groups if g['candidate_category'] is None),
                'deferred_items': sum(l['action'] == 'reject' for l in latest.values()),
                'hypothesis_groups': dict(Counter(g['hypothesis'] for g in groups)),
                'recurring_candidate_groups': sum(g['recurring_candidate'] for g in groups),
                'pending_category_items': sum(g['count'] for g in groups),
                'human_labels': sum(l['action'] == 'confirm' for l in latest.values()),
                'actual_owner_actions_eliminated': 0, 'paid_calls': 0, 'database_modified': False,
                'financial_apply_allowed': False, 'rules_revision': rules_revision()}

    def decide_category_group(self, group_hash: str, *, actor: str, reason: str, confirmed: bool,
                              action: str, category: str | None = None) -> dict[str, Any]:
        """Confirm a bounded homogeneous category cohort atomically through HumanLabels."""
        if confirmed is not True or action not in {'confirm', 'reject'} or not actor.strip() or not reason.strip():
            raise ValueError('Explicit category group confirmation required')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.store._audit(conn)
            preview = self.category_review()
            group = next((g for g in preview['groups'] if g['group_hash'] == group_hash), None)
            if not group or not group['eligible_one_owner_decision']:
                raise ValueError('Stale or heterogeneous category group')
            # Corrections remain explicit and cannot change direction into income/investment/transfer.
            from jarvis.finance.event_taxonomy import (
                NON_ECONOMIC_CATEGORIES,
                canonical_category,
            )
            allowed = {canonical_category(c) for k,v in OUTGOING_CLASSES.items() for c in [k,*v['aliases']]} - NON_ECONOMIC_CATEGORIES
            if action == 'confirm' and (category not in allowed or category in {'unknown','other','investment','savings','cash'}):
                raise ValueError('Unsupported grouped economic category')
            events = []
            for member in group['members']:
                event = HumanLabels(self.store).decide(member['item_id'], action, actor=actor, reason=reason,
                    expected_revision=member['label_revision'], category=category if action == 'confirm' else None,
                    _connection=conn, _group_hash=group_hash)
                events.append(event['seq'])
            conn.commit()
        return {'status': 'recorded', 'group_hash': group_hash, 'label_revisions': events, 'recorded': len(events),
                'atomic': True, 'production_database_modified': False, 'financial_apply_allowed': False}

    def group_preview(self) -> dict[str, Any]:
        """Preflight all current PDF scopes; evidence is diagnostic, never authority."""
        from jarvis.finance.bank.sber_provenance import pdf_provenance
        from jarvis.finance.statement_diagnostics import bound_warning_evidence

        with review_snapshot(self.store) as frozen:
            sources = self.sources(frozen)
            if not sources or len(sources) > 4 or any(s['format'] != 'pdf' for s in sources):
                raise ValueError('Expected one to four prepared PDF scopes')
            if len({s['sha256'] for s in sources}) != len(sources):
                raise ValueError('Duplicate originals need manual review')
            cards = []
            for source in sources:
                meta, rows = source['metadata'], source['transactions']
                start, end = map(date.fromisoformat, meta['period'])
                if (not meta.get('bank_account') or not meta.get('local_account') or meta.get('mapping_status') != 'mapped'
                        or not rows or start > end or any(row['currency'] != meta['currency']
                            or not str(start) <= row['tx_date'] <= str(end) for row in rows)):
                    raise ValueError('Source account/currency/period binding unavailable')
                discrepancy = finite_money(meta['closing']) - finite_money(meta['opening']) - sum(
                    (finite_money(row['amount']) for row in rows), Decimal(0))
                if discrepancy != 0:
                    raise ValueError('Balance mismatch')
                if any(other['id'] != source['id'] and other['metadata']['local_account'] == meta['local_account']
                       and other['metadata']['currency'] == meta['currency']
                       and other['metadata']['period'][0] <= meta['period'][1]
                       and other['metadata']['period'][1] >= meta['period'][0] for other in sources):
                    raise ValueError('Overlapping scopes need manual review')
                lineage = pdf_provenance(self._folder('statement-originals') / source['original'], account=meta['bank_account'])
                if (lineage['source_sha256'] != source['sha256'] or lineage['parsed_count'] != len(rows)
                        or lineage['unbound_count'] or any(r['parsed_transaction_sha256'] != digest(rows[r['ordinal']])
                                                         for r in lineage['rows'])):
                    raise ValueError('Original payload binding mismatch')
                evidence = bound_warning_evidence(self, source)
                cards.append({'id': source['id'], 'sha256': source['sha256'], 'revision': source['revision'],
                    'parser_version': source['parser_version'], 'accepted': source['accepted'],
                    'account_alias': '••' + str(meta['local_account'])[-4:], 'period': meta['period'],
                    'currency': meta['currency'], 'count': len(rows), 'discrepancy': str(discrepancy),
                    'warning_count': len(source['parser_warnings']), 'warning_evidence': evidence,
                    'mapping_binding': digest([meta['bank_account'], meta['local_account']]),
                    'payload_binding': digest(rows)})
            pending = [c for c in cards if not c['accepted']]
            reasons = []
            if not pending:
                reasons.append('NO_PENDING_SOURCES')
            if any(c['warning_evidence']['status'] not in {'verified_diagnostic', 'not_required'} for c in pending):
                reasons.append('WARNING_EVIDENCE_UNAVAILABLE')
            items = {_item_key(i): i for i in frozen.items()}
            for source in sources:
                for n, row in enumerate(source['transactions']):
                    item = items.get(_item_key({'source_sha256': source['sha256'], 'pdf_index': n}))
                    if item and any(str(item['operation'].get(f)) != str(row.get(f))
                                    for f in ('tx_date', 'amount', 'currency', 'merchant', 'description')):
                        reasons.append('SOURCE_PAYLOAD_CONFLICT')
                        break
            binding = digest([cards, frozen.metadata()['packet_hash'], frozen.audit(), HumanLabels(frozen).audit()])
            return {'status': 'ready_for_owner' if not reasons else 'blocked', 'binding': binding,
                    'sources': cards, 'pending_count': len(pending), 'blocked_reasons': sorted(set(reasons)),
                    'accepted_observations': sum(c['count'] for c in cards if c['accepted']),
                    'pending_observations': sum(c['count'] for c in pending),
                    'source_payload_conflict': 'SOURCE_PAYLOAD_CONFLICT' in reasons,
                    'legacy_conflicts': 'UNKNOWN; existing reconciliation queue preserved; no resolution',
                    'production_database_modified': False, 'database_modified': False,
                    'categories_confirmed': False, 'coverage_complete': False, 'financial_apply_allowed': False}

    def accept_group(self, binding: str, *, actor: str, confirmed: bool,
                     acknowledge_parser_warnings: bool) -> dict[str, Any]:
        """Use existing sequential audited decisions; never claim group atomicity."""
        from jarvis.finance.review_backup import (
            create_review_backup,
            restore_review_backup,
        )

        if (confirmed is not True or acknowledge_parser_warnings is not True or not actor.strip()
                or not re.fullmatch('[0-9a-f]{64}', binding)):
            raise ValueError('Explicit owner source/warning confirmation required')
        receipt_key = 'statement_group:' + binding
        self.store.audit()
        saved = self.store.metadata().get(receipt_key)
        if saved:
            return {**json.loads(saved), 'replayed': True, 'historical_result': True,
                    'current_sources': [{'id': s['id'], 'accepted': s['accepted'], 'revision': s['revision']}
                                        for s in self.sources()]}
        preview = self.group_preview()
        if preview['binding'] != binding or preview['status'] != 'ready_for_owner':
            raise ValueError('Stale or blocked group preview')
        backup = create_review_backup(self.store)
        # Restore verification is required before any audited source decision.
        with tempfile.TemporaryDirectory(prefix='jarvis-group-recovery-') as temporary:
            restored = Path(temporary) / 'restored'
            restore_review_backup(Path(backup['archive']), restored)
            if ReviewStore(restored).audit() != self.store.audit():
                raise ValueError('Backup audit differs from current state')
        fresh = self.group_preview()
        if fresh['binding'] != binding:
            raise ValueError('State changed during backup')
        result = {'status': 'IN_PROGRESS', 'binding': binding, 'sources': [], 'replayed': False,
                  'interruption_policy': 'Read source audit; do not automatically retry an incomplete group',
                  'backup_sha256': hashlib.sha256(Path(backup['archive']).read_bytes()).hexdigest(),
                  'backup_id': Path(backup['archive']).name,
                  'atomic': False, 'private_review_modified': True, 'database_modified': False,
                  'production_database_modified': False, 'financial_apply_allowed': False}
        # Claim once under the same SQLite writer lock used by ReviewStore decisions.
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            events = self.store._audit(conn)
            saved_row = conn.execute('SELECT value FROM review_meta WHERE key=?', (receipt_key,)).fetchone()
            if saved_row:
                return {**json.loads(saved_row[0]), 'replayed': True}
            # A concurrent audited mutation invalidates the previously verified snapshot.
            # Include the complete audit in the preview binding; compare under lock.
            if digest([fresh['sources'], self.store.metadata()['packet_hash'], events, HumanLabels(self.store).audit()]) != binding:
                raise ValueError('Concurrent review or label update')
            self.store._append_event(conn, {'seq': len(events) + 1, 'domain': 'statement_group',
                'item_id': binding, 'action': 'start_group', 'actor': actor.strip(),
                'timestamp': datetime.now(UTC).isoformat(), 'backup_sha256': result['backup_sha256'],
                'receipt_sha256': digest(result), 'source_preflight': fresh['sources']})
            conn.execute('INSERT INTO review_meta VALUES (?,?)', (receipt_key, encoded(result)))
            conn.commit()
        outcomes = []
        for card in fresh['sources']:
            if card['accepted']:
                outcomes.append({'id': card['id'], 'status': 'already_accepted', 'revision': card['revision']})
                continue
            try:
                # Refuse changed originals/evidence/revisions between individual decisions.
                current = next(s for s in self.sources() if s['id'] == card['id'])
                from jarvis.finance.statement_diagnostics import bound_warning_evidence
                if (current['revision'] != card['revision'] or current['accepted']
                        or bound_warning_evidence(self, current) != card['warning_evidence']):
                    raise ValueError('Source changed during group')
                event = self.decide(card['id'], actor=actor, expected_revision=card['revision'], accept=True,
                                    acknowledge_parser_warnings=True)
                outcomes.append({'id': card['id'], 'status': 'accepted', 'revision': event['seq']})
            except (ValueError, KeyError, OSError, sqlite3.Error):
                outcomes.append({'id': card['id'], 'status': 'failed_refresh_required'})
                break
        processed = {o['id'] for o in outcomes}
        outcomes.extend({'id': c['id'], 'status': 'not_attempted'} for c in fresh['sources'] if c['id'] not in processed)
        result.update(status='SUCCESS' if all(o['status'] in {'accepted', 'already_accepted'} for o in outcomes)
                      else 'PARTIAL', sources=outcomes)
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            events = self.store._audit(conn)
            self.store._append_event(conn, {'seq': len(events) + 1, 'domain': 'statement_group',
                'item_id': binding, 'action': 'finish_group', 'actor': actor.strip(),
                'timestamp': datetime.now(UTC).isoformat(), 'result': result, 'receipt_sha256': digest(result)})
            conn.execute('UPDATE review_meta SET value=? WHERE key=?', (encoded(result), receipt_key))
            conn.commit()
        return result

    def project(self) -> dict[str, Any]:
        """Build an immutable derived FinanceDB; labels never accept a bank source."""
        with review_snapshot(self.store) as frozen:
            sources = [s for s in self.sources(frozen) if s['accepted']]
            if not sources:
                raise ValueError('Owner source acceptance required; previews are not accepted money')
            version = self._binding(frozen)
            folder = self._folder('financial-snapshots')
            folder.mkdir(exist_ok=True)
            path = folder / (version + '.sqlite')
            if path.is_symlink():
                raise ValueError('Unsafe snapshot path')
            entry = frozen.metadata().get('statement_projection:' + version)
            if entry:
                meta = json.loads(entry)
                proof = next((e for e in frozen.audit() if e.get('domain') == 'statement_projection'
                              and e['item_id'] == 'statement_projection:' + version), {})
                if (proof.get('metadata_sha256') != digest(meta) or meta['version'] != version or not path.is_file()
                        or hashlib.sha256(path.read_bytes()).hexdigest() != meta['sha256']):
                    raise ValueError('Financial snapshot changed; no fallback to old archive')
                return {**meta, 'path': str(path)}
            if path.exists():
                raise ValueError('Interrupted snapshot needs inspection; no overwrite')
            labels = HumanLabels(frozen).latest()
            with tempfile.TemporaryDirectory(prefix='jarvis-statement-ledger-') as temporary:
                db = FinanceDB(Path(temporary) / 'ledger.sqlite')
                db.init()
                mapping = []
                for source in sources:
                    scope = source['metadata']
                    origin = 'statement:' + source['sha256']
                    for n, row in enumerate(source['transactions']):
                        key = _item_key({'source_sha256': source['sha256'], 'pdf_index': n})
                        label = labels.get(key)
                        category = economic_category(row, label)
                        ident = db.add_transaction(Transaction(date.fromisoformat(row['tx_date']), finite_money(row['amount']),
                            row['currency'], row['description'], row['merchant'], category, scope['local_account'],
                            f"pdf:{source['sha256']}:{n}"), source=origin, parser_version=source['parser_version'])
                        if not ident:
                            raise ValueError('Statement row not projected; inspect duplicate provenance')
                        mapping.append({'transaction_id': ident, 'review_id': key, 'source_id': source['id'], 'ordinal': n + 1,
                                        'label_revision': label['seq'] if label else 0, 'original_record': row})
                    start, end = map(date.fromisoformat, scope['period'])
                    db.record_account_balance(scope['local_account'], scope['currency'], start - timedelta(days=1), finite_money(scope['opening']), origin)
                    db.record_account_balance(scope['local_account'], scope['currency'], end, finite_money(scope['closing']), origin)
                content = db.path.read_bytes()
                sha = hashlib.sha256(content).hexdigest()
                meta = {'version': version, 'sha256': sha, 'sources': [s['id'] for s in sources], 'lineage': mapping,
                    'created_at': datetime.now(UTC).isoformat(), 'production_database_modified': False}
                with self.store.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    events = self.store._audit(conn)
                    if self._binding(self.store) != version:
                        raise ValueError('Concurrent source/category update; refresh')
                    # Exclusive publish; no production destination argument exists.
                    with path.open('xb') as output:
                        output.write(content)
                    conn.execute('INSERT INTO review_meta VALUES (?,?)', ('statement_projection:' + version, encoded(meta)))
                    self.store._append_event(conn, {'seq': len(events) + 1, 'domain': 'statement_projection',
                        'item_id': 'statement_projection:' + version, 'action': 'project_statements', 'sha256': sha,
                        'metadata_sha256': digest(meta), 'timestamp': meta['created_at']})
                    conn.commit()
        return {**meta, 'path': str(path)}

    def month_report(self, month: str, currency: str = 'RUB', *, now: datetime | None = None) -> dict[str, Any]:
        """Bound a user month view to available historical dates and disclose its period."""
        if not re.fullmatch(r'\d{4}-\d{2}', month):
            raise ValueError('Explicit month required')
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError('Aware report clock required')
        start = date.fromisoformat(month + '-01')
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        dates = [date.fromisoformat(s['metadata']['period'][1]) for s in self.sources()
                 if s['accepted'] and s['metadata']['currency'] == currency]
        if not dates:
            raise ValueError('Accepted currency source required')
        return self.report(start, min(end, current.date(), max(dates)), currency, now=current)

    def report(self, start: date, end: date, currency: str = 'RUB', *, accounts: tuple[str, ...] | None = None,
               now: datetime | None = None) -> dict[str, Any]:
        """Report source-scoped bank movement, withholding incomplete economic totals."""
        current = now or datetime.now(UTC)
        if current.tzinfo is None or end < start or end > current.date() or not re.fullmatch('[A-Z]{3}', currency):
            raise ValueError('Valid historical period/currency required')
        projected = self.project()
        all_sources = self.sources()
        observed = [s for s in all_sources if s['metadata']['currency'] == currency]
        if accounts is not None:
            if not accounts or len(set(accounts)) != len(accounts) or not set(accounts) <= {s['metadata']['local_account'] for s in all_sources}:
                raise ValueError('Unknown or ambiguous account scope')
            observed = [s for s in observed if s['metadata']['local_account'] in accounts]
        sources = [s for s in observed if s['accepted']]
        with sqlite3.connect(Path(projected['path']).as_uri() + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute('SELECT * FROM transactions WHERE currency=? AND tx_date BETWEEN ? AND ? ORDER BY tx_date,id', (currency, start.isoformat(), end.isoformat()))]
        if accounts is not None:
            rows = [r for r in rows if r['account'] in accounts]
        flow = cashflow(rows, currency)
        covered = sorted({s['metadata']['local_account'] for s in sources if s['metadata']['period'][0] <= start.isoformat() and s['metadata']['period'][1] >= end.isoformat()})
        known_accounts = sorted(accounts if accounts is not None else {s['metadata']['local_account'] for s in observed})
        unresolved = sum(r['category'] == 'pending_review' for r in rows)
        complete = bool(known_accounts) and known_accounts == covered
        healthy = complete and bool(rows) and not unresolved
        incoming = sum((finite_money(r['amount']) for r in rows if finite_money(r['amount']) > 0), Decimal(0))
        outgoing = sum((-finite_money(r['amount']) for r in rows if finite_money(r['amount']) < 0), Decimal(0))
        lineage = {m['transaction_id']: m for m in projected['lineage']}
        known_operations = [{'review_id': lineage[r['id']]['review_id'], 'date': r['tx_date'], 'amount': r['amount'],
            'currency': currency, 'category': r['category'], 'account_alias': '••' + str(r['account'])[-4:]}
            for r in rows if r['category'] != 'pending_review']
        if (self._binding(self.store) != projected['version']
                or hashlib.sha256(Path(projected['path']).read_bytes()).hexdigest() != projected['sha256']):
            raise ValueError('Source/category revision changed; refresh report')
        return {'status': 'available' if healthy else 'insufficient_data', 'period': [start.isoformat(), end.isoformat()],
            'currency': currency, 'version': projected['version'], 'source_sha256': projected['sha256'],
            'scope': 'accepted_statements_only', 'closed_month': end < current.date().replace(day=1),
            'full_calendar_month': start.day == 1 and end == (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1),
            'operation_count': len(rows), 'unresolved_categories': unresolved, 'coverage_complete': complete,
            'unaccepted_sources': sum(not s['accepted'] for s in observed),
            'covered_accounts': len(covered), 'known_accounts': len(known_accounts), 'sources': [s['id'] for s in sources],
            'account_scope': known_accounts, 'audit_revision': len(self.store.audit()),
            'label_revision': len(HumanLabels(self.store).audit()),
            'source_refs': [{'source_id': s['id'], 'sha256': s['sha256'], 'parser_version': s['parser_version'],
                'revision': s['revision'], 'account': s['metadata']['local_account'], 'period': s['metadata']['period']} for s in sources],
            'known_by_category': {k: str(v) for k, v in flow['by_category'].items()},
            'known_operations': known_operations[:100], 'known_operation_count': len(known_operations),
            'known_income': str(flow['income']), 'known_expenses': str(flow['expenses']),
            'unknown_credits': str(sum((finite_money(r['amount']) for r in rows if r['category'] == 'pending_review' and finite_money(r['amount']) > 0), Decimal(0))),
            'unknown_debits': str(sum((-finite_money(r['amount']) for r in rows if r['category'] == 'pending_review' and finite_money(r['amount']) < 0), Decimal(0))),
            'accounts': [{'account': account, 'credits': str(sum((finite_money(r['amount']) for r in rows if r['account'] == account and finite_money(r['amount']) > 0), Decimal(0))),
                'debits': str(sum((-finite_money(r['amount']) for r in rows if r['account'] == account and finite_money(r['amount']) < 0), Decimal(0)))} for account in covered],
            'bank_movements': {'credits': str(incoming), 'debits': str(outgoing), 'net': str(incoming - outgoing),
                               'notice': 'Движение по выпискам; переводы включены. Это не заработок и не экономические расходы.'} if sources else None,
            'income': str(flow['income']) if healthy else None, 'expenses': str(flow['expenses']) if healthy else None,
            'net': str(flow['net']) if healthy else None,
            'by_category': {k: str(v) for k, v in flow['by_category'].items()} if healthy else {},
            'available_funds': None, 'balance_notice': 'Остатки выписок исторические, текущие доступные деньги не подтверждены.',
            'reason': None if healthy else 'Нужны полное покрытие выбранных счетов и подтверждённые экономические категории.',
            'formula': 'Ledger Decimal cashflow; only explicitly confirmed compatible income/expense categories. Transfers/refunds/principal need linked evidence and remain pending.',
            'production_database_modified': False, 'financial_apply_allowed': False}

    def pending_source_review(self) -> dict[str, Any]:
        """Group source-scoped provisional evidence without decisions or ledger apply."""
        from collections import Counter

        from jarvis.finance.bank.sber_provenance import pdf_provenance
        from jarvis.finance.classification.suggestions import (
            LocalSuggestions,
            rules_revision,
        )
        from jarvis.finance.statement_diagnostics import bound_warning_evidence
        with review_snapshot(self.store) as frozen:
            sources = self.sources(frozen)
            pending = [source for source in sources if not source['accepted']]
            if sum(len(source['transactions']) for source in pending) > 1000:
                raise ValueError('Bounded source review required')
            items = {row['id']: row for row in frozen.items()}
            suggestions = LocalSuggestions(frozen)
            groups: dict[str, dict[str, Any]] = {}
            scopes = []
            proposals = conflicts = missing = 0
            for source in pending:
                rows = source['transactions']
                payloads = Counter(digest(row) for row in rows)
                repeated = sum(count > 1 for count in payloads.values())
                meta = source['metadata']
                movement = sum((finite_money(row['amount']) for row in rows), Decimal(0))
                discrepancy = finite_money(meta['closing']) - finite_money(meta['opening']) - movement
                provenance = None
                if source['format'] == 'pdf':
                    provenance = pdf_provenance(self._folder('statement-originals') / source['original'], account=meta['bank_account'])
                    if provenance['source_sha256'] != source['sha256'] or provenance['parsed_count'] != len(rows) or any(
                            row['parsed_transaction_sha256'] != digest(rows[row['ordinal']]) for row in provenance['rows']):
                        raise ValueError('Original-row review binding differs')
                warning_binding = bound_warning_evidence(self, source)
                scopes.append({'id': source['id'], 'account_alias': '••'+str(meta['local_account'])[-4:],
                    'period': meta['period'], 'currency': meta['currency'], 'count': len(rows),
                    'sha256': source['sha256'], 'revision': source['revision'], 'discrepancy': str(discrepancy),
                    'parser_warning_count': len(source['parser_warnings']),
                    'warning_explanation_status': warning_binding['status'], 'warning_explanations': warning_binding['entries'],
                    'warning_notice': ('Объяснения привязаны к исходному SHA/парсеру/ревизии; warnings сохранены. Это диагностика, не согласие.'
                        if warning_binding['status'] == 'verified_diagnostic' else
                        'Проверяемое объяснение недоступно; warnings сохранены. Сверьте оригинал.'),
                    'duplicate_like_groups': repeated, 'provenance_bound': len(provenance['rows']) if provenance else None,
                    'provenance_unbound': provenance['unbound_count'] if provenance else None,
                    'after_acceptance': 'Источник войдёт в accepted projection; категории останутся pending до отдельных human labels. Production import не выполняется.',
                    'owner_question': 'Подтвердите mapping, период и принадлежность документа; полнота истории не доказана.',
                    'limitations': ['zero_discrepancy_is_not_completeness', 'repeated_payload_not_economic_duplicate', 'historical_not_current_balance']})
                for n, row in enumerate(rows):
                    item_id = _item_key({'source_sha256': source['sha256'], 'pdf_index': n})
                    item = items.get(item_id)
                    payload_conflict = bool(item and any(str(item['operation'].get(field)) != str(row.get(field))
                        for field in ('tx_date', 'amount', 'currency', 'merchant', 'description')))
                    proposal = suggestions._preview_frozen(frozen, item_id) if item and not payload_conflict else None
                    category = proposal.suggested_category if proposal else None
                    reason = proposal.reason_code if proposal else ('source_payload_conflict' if payload_conflict else 'review_item_not_materialized')
                    proposals += category is not None
                    missing += item is None
                    conflicts += payload_conflict or bool(item and item.get('conflict_reasons'))
                    text = str(row['description']).casefold()
                    amount = finite_money(row['amount'])
                    bucket = ('conflict' if payload_conflict or item and item.get('conflict_reasons') else
                        'candidate_refund' if amount > 0 and any(word in text for word in ('возврат', 'refund')) else
                        'candidate_transfer' if any(word in text for word in ('перевод', 'transfer')) else
                        'deterministic_proposal' if category else 'unresolved_economic_identity')
                    signature = digest([' '.join(str(row['merchant']).casefold().split()), row['currency'], bucket, category, reason])
                    group = groups.setdefault(signature, {'group_id': signature, 'kind': bucket, 'reason': reason,
                        'category_proposal': category, 'currency': row['currency'], 'count': 0, 'examples': [],
                        'dates_by_amount': {}, 'source_ids': [], 'apply_allowed': False, 'human_label': False})
                    group['count'] += 1
                    if len(group['examples']) < 3:
                        group['examples'].append({'item_id': item_id if item else None, 'source_id': source['id'], 'ordinal': n})
                    if source['id'] not in group['source_ids']:
                        group['source_ids'].append(source['id'])
                    group['dates_by_amount'].setdefault(str(amount), set()).add(row['tx_date'])
            for group in groups.values():
                group['recurring_candidate'] = any(len(dates) >= 3 and
                    (date.fromisoformat(max(dates)) - date.fromisoformat(min(dates))).days >= 28 for dates in group.pop('dates_by_amount').values())
                group['recurrence_confirmed'] = False
            return {'status': 'provisional', 'scope': 'pending_sources_only', 'sources': scopes,
                'groups': sorted(groups.values(), key=lambda group: (-group['count'], group['group_id'])),
                'revision': digest([[(source['id'], source['revision']) for source in sources], HumanLabels(frozen).audit(), rules_revision()]),
                'pending_source_decisions': len(scopes), 'pending_observations': sum(scope['count'] for scope in scopes),
                'accepted_source_observations': sum(len(source['transactions']) for source in sources if source['accepted']),
                'review_groups': len(groups), 'source_conflict_rows': conflicts, 'missing_lineage': missing,
                'safe_deterministic_proposals': proposals, 'ai_proposals': 0, 'automatic_actions_authorized': 0,
                'actual_unresolved_questions': 'UNKNOWN; navigation groups are not authorized decision units',
                'actual_owner_actions_eliminated': 0, 'human_labels': sum(label['action'] == 'confirm' for label in HumanLabels(frozen).latest().values()),
                'training_allowed': False, 'financial_apply_allowed': False, 'database_modified': False}

    def question(self, scope: AcceptedStatementScope, *, intent: str, currency: str,
                 now: datetime | None = None) -> dict[str, Any]:
        """Extend financial-question with narrow accepted evidence, not full-ledger authority."""
        if intent not in {'bank_movement', 'income', 'spending', 'food', 'available_funds', 'missing_data'}:
            raise ValueError('Unsupported statement question')
        sources = self.sources()
        base = {'intent': intent, 'scope': 'accepted_statements_only', 'period': [str(scope.start), str(scope.end)],
                'currency': currency, 'answer': None, 'database_modified': False, 'apply_allowed': False,
                'bank_authenticity_verified': False, 'production_database_modified': False}
        if not any(s['accepted'] for s in sources):
            revoked = any(e.get('domain') == 'statement_source' for e in self.store.audit())
            return {**base, 'status': 'unavailable' if revoked else 'awaiting_owner',
                    'reason_code': 'SOURCE_REVOKED' if revoked else 'AWAITING_OWNER',
                    'reason': 'Откройте «Сбербанк и источники» → «Мои выписки»: проверьте mapping, период, остатки и предупреждения, затем явно примите источник.',
                    'snapshot_accepted': False}
        report = self.report(scope.start, scope.end, currency, accounts=scope.accounts, now=now)
        if scope.expected_version is not None and scope.expected_version != report['version']:
            return {**base, 'status': 'unavailable', 'reason_code': 'STALE_SNAPSHOT',
                    'reason': 'Версия источника или категории изменилась; запросите новый расчёт.', 'snapshot_accepted': False}
        result = {**report, **base, 'snapshot_accepted': True, 'snapshot_version': report['version'],
                  'calculation_id': digest([report['version'], scope.model_dump(mode='json'), currency, intent]),
                  'status': report['status']}
        if intent == 'bank_movement':
            result.update(answer=report['bank_movements'] if report['coverage_complete'] else None,
                          status='available' if report['coverage_complete'] else 'insufficient_data')
        elif intent == 'income':
            result['answer'] = report['income']
        elif intent == 'spending':
            result['answer'] = report['expenses']
        elif intent == 'food':
            known = report['known_by_category']
            # Groceries are narrower than cafes/delivery; do not merge unrelated food semantics.
            total = sum((finite_money(v) for k, v in known.items() if k in {'food', 'food_basic'}), Decimal(0))
            result.update(known_category_total=str(total) if report['source_refs'] else None, category_scope=['food', 'food_basic'],
                          category_operations=[r for r in report['known_operations'] if r['category'] in {'food', 'food_basic'}],
                          operation_list_limit=100,
                          answer=str(total) if report['status'] == 'available' else None)
        elif intent == 'available_funds':
            result.update(status='insufficient_data', reason='Нет отдельных свежих принятых остатков, резерва и обязательств. Можно задать гипотетический сценарий явно.')
        if self._binding(self.store) != report['version']:
            raise ValueError('Concurrent source/category update; discard question answer')
        return result
