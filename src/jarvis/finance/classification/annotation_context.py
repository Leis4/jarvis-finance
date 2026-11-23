"""Local context recovery; only reviewed allowlisted constants may leave the preview."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jarvis.finance.classification.annotation import AnnotationExchange
from jarvis.finance.classification.teacher import normalize_teacher_merchant
from jarvis.finance.ml_local.merchant_kb import MERCHANT_KB
from jarvis.finance.review_backup import review_snapshot
from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_store import ReviewStore
from jarvis.finance.statement_product import StatementProduct, digest

CONTEXT_VERSION = 'local-annotation-context-v1'
# This independent extraction policy does not change accepted-annotation-v1 hashes.
EXTRA_MERCHANTS = {
    'mts': ('mts', 'мтс'), 'megafon': ('megafon', 'мегафон'),
    'beeline': ('beeline', 'билайн'), 'tele2': ('tele2', 'теле2'),
    'rostelecom': ('rostelecom', 'ростелеком'), 'telegram': ('telegram',),
    'google play': ('google play', 'googleplay'), 'apple services': ('apple.com/bill',),
    'tatneft': ('tatneft', 'татнефть'), 'lukoil': ('lukoil', 'лукойл'),
    'gazpromneft': ('gazpromneft', 'газпромнефть'), 'fix price': ('fix price', 'fixprice'),
    'dns': ('dns',), 'citilink': ('citilink', 'ситилинк'), 'mvideo': ('mvideo', 'м.видео'),
    'lamoda': ('lamoda',), 'aliexpress': ('aliexpress',), 'sportmaster': ('sportmaster', 'спортмастер'),
    'yookassa': ('yookassa', 'юкасса'), 'robokassa': ('robokassa', 'робокасса'),
    'cloudpayments': ('cloudpayments',), 'unitpay': ('unitpay',),
}
PURPOSES = {
    'Bank text: interest accrual': r'начислен\w* процент|выплат\w* процент|процент\w* по вклад',
    'Bank text: salary reference': r'заработн\w* плат|выплат\w* зарплат',
    'Bank text: stipend reference': r'стипенди',
    'Bank text: commission reference': r'комисси',
    'Bank text: purchase refund reference': r'возврат покупк|возврат средств за товар',
    'Bank text: cash withdrawal': r'выдача наличных|снятие наличных',
    'Bank text: cash deposit': r'внесение наличных',
    'Bank text: account movement reference; ownership unknown': r'karta-vklad|vklad-karta|карта-вклад|вклад-карта',
}
TRANSFER = re.compile(r'(?i)перевод|transfer|c2c|p2p')
INJECTION = re.compile(r'(?i)ignore|system\s*:|assistant\s*:|инструкци|игнорир|<script|shell|tool_call|```')
SECRETS = re.compile(r'(?i)sk-|gh[pousr]_|\b(?:pin|otp|password|cookie|authorization|пароль|пин|токен|token|secret|session|bearer|api.key)\b')
CONTACT = re.compile(r'(?i)@|https?://|\b(?:улица|ул\.|проспект|пр-т|квартира|кв\.|дом\s+\d|street|address)')
PERSON = re.compile(r'(?i)\b(?:получател\w*|отправител\w*|физическ\w* лиц\w*|фио|ип\s|ip\s)')
NAMES = re.compile(r'\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}|\b[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\.')


class ContextFieldReview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    item_id: str = Field(pattern=r'^[0-9a-f]{24}$')
    source_binding: str = Field(pattern=r'^[0-9a-f]{64}$')
    field: Literal['merchant', 'purpose']
    value: str = Field(min_length=1, max_length=120)
    approved: bool = Field(strict=True)
    public_nonpersonal_confirmed: bool = Field(strict=True)

    @model_validator(mode='after')
    def explicit_field_consent(self) -> ContextFieldReview:
        if self.approved is not True or self.public_nonpersonal_confirmed is not True:
            raise ValueError('Explicit nonpersonal field attestation required')
        return self


class ContextExportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    preview_binding: str = Field(pattern=r'^[0-9a-f]{64}$')
    actor: str = Field(min_length=1, max_length=100)
    confirmed: bool = Field(strict=True)
    fields: list[ContextFieldReview] = Field(max_length=2000)
    holdout_groups: list[str] = Field(default_factory=list, max_length=1000)


def _has(text: str, pattern: str) -> bool:
    return bool(re.search(r'(?<!\w)' + re.escape(pattern) + r'(?!\w)', text, re.IGNORECASE))


def recover_context(operation: dict[str, Any]) -> dict[str, Any]:
    """Return public constants and a separate bounded owner-local text, never a label."""
    values = [operation.get(k) for k in ('merchant', 'description')]
    if any(v is not None and not isinstance(v, str) for v in values):
        return {'status': 'NEEDS_OWNER_LOCAL_REVIEW', 'reason': 'broken_description',
                'merchant': None, 'purpose': None, 'local_purpose': None, 'privacy_rejected': True}
    raw = ' '.join(v or '' for v in values)
    if len(raw) > 4000 or any(ord(c) < 32 and c not in '\r\n\t' or 0xD800 <= ord(c) <= 0xDFFF or c == '\ufffd' for c in raw):
        return {'status': 'NEEDS_OWNER_LOCAL_REVIEW', 'reason': 'broken_description',
                'merchant': None, 'purpose': None, 'local_purpose': None, 'privacy_rejected': True}
    cleaned = []
    for value in values:
        text = normalize_teacher_merchant(value or '')
        # Accepted parser can truncate the duplicated merchant/description at this suffix.
        text = re.sub(r'(?i)[.\s]+операция\s+по(?:\s+(?:карте|счету|счёту))?[.\s]*$', '', text)
        text = re.sub(r'(?i)\b(YM|Yandex)\*\d+\*', r'\1*', text)
        text = re.sub(r'(?i)\s+(?:(?:MOSCOW|MOSKVA|KAZAN|SANKT-PETERBURG)\s+)?RUS\.?$', '', text)
        if text and text not in cleaned:
            cleaned.append(text)
    clean = ' | '.join(cleaned)
    aliases: dict[str, tuple[str, ...]] = {
        e.patterns[0]: tuple(p for p in e.patterns if not re.search(r'(?i)\bip\b|\bип\b', p))
        for e in MERCHANT_KB if not re.search(r'(?i)\bip\b|\bип\b', e.patterns[0])}
    aliases.update(EXTRA_MERCHANTS)
    # Names cannot be authorized by matching an unrelated safe brand nearby.
    residual = clean
    for patterns in aliases.values():
        for pattern in sorted(patterns, key=len, reverse=True):
            residual = re.sub(r'(?<!\w)' + re.escape(pattern) + r'(?!\w)', ' ', residual, flags=re.IGNORECASE)
    named = NAMES.search(residual) or re.search(
        r'\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}|\b[A-ZА-ЯЁ]{3,}\s+[A-ZА-ЯЁ]{3,}', residual)
    # A recognized suffix is removed before detection; arbitrary card fragments are refused.
    reason = ('malicious_text' if INJECTION.search(raw) else 'secret' if SECRETS.search(raw)
              else 'contact_or_address' if CONTACT.search(clean) else 'bank_or_numeric_identifier' if re.search(r'\d|[*•]{2,}', residual)
              else 'person_or_recipient' if PERSON.search(clean) or named else None)
    transfer = bool(TRANSFER.search(raw))
    if reason:
        return {'status': 'NEEDS_OWNER_LOCAL_REVIEW', 'reason': reason, 'merchant': None, 'purpose': None,
                'local_purpose': 'Transfer mentioned; recipient and economic intent require local owner review.' if transfer else None,
                'privacy_rejected': True}
    hits = {canonical for canonical, patterns in aliases.items() if any(_has(clean, p) for p in patterns)}
    purposes = [name for name, pattern in PURPOSES.items() if re.search(pattern, clean, re.IGNORECASE)]
    merchant = next(iter(hits)) if len(hits) == 1 else None
    purpose = purposes[0] if len(purposes) == 1 else None
    if transfer and purpose is None:
        return {'status': 'NEEDS_OWNER_LOCAL_REVIEW', 'reason': 'transfer_intent_unknown',
                'merchant': None, 'purpose': None,
                'local_purpose': 'Transfer mentioned; amount/direction do not establish income, payment or ownership.',
                'privacy_rejected': False}
    # Unknown business-shaped tokens remain LOCAL until the owner explicitly adds
    # this exact source-bound field to a one-export allowlist. Never raw free text.
    local_business = next((t for t in cleaned if re.fullmatch(r'[A-Z][A-Z.*&/_-]{2,59}', t)), None)
    return {'status': 'SAFE_CANDIDATE' if merchant or purpose else 'LOCAL_CANDIDATE' if local_business else 'UNKNOWN',
            'reason': 'allowlisted_text' if merchant or purpose else 'owner_business_attestation_required' if local_business else 'no_allowlist_match',
            'merchant': merchant, 'purpose': purpose,
            'local_merchant': local_business, 'local_purpose': clean[:500] if clean else None, 'privacy_rejected': False}


class AnnotationContext:
    """Read accepted originals through existing bindings; persist no review decisions."""
    def __init__(self, store: ReviewStore):
        self.store = store

    def _build(self, frozen: ReviewStore) -> tuple[dict[str, Any], dict[str, Any]]:
        dataset = AnnotationExchange(self.store)._dataset(frozen, private=True)
        sources = StatementProduct(self.store).sources(frozen)
        originals = {(s['id'], n + 1): op for s in sources if s['accepted'] for n, op in enumerate(s['transactions'])}
        local_rows, safe_rows = [], []
        for row in dataset['rows']:
            operation = originals[(row['source_id'], row['original_ordinal'])]
            context = recover_context(operation)
            fields = {k: context[k] for k in ('merchant', 'purpose')}
            safe = {k: v for k, v in row.items() if k not in {'observed_date', 'signed_amount', 'account_alias'}}
            # Even existing KB hints do not bypass the stricter context/privacy boundary.
            safe.update(public_merchant_hint=None, semantic_candidate=fields,
                        context_status=context['status'], context_reason=context['reason'],
                        owner_field_reviewed=False)
            safe_rows.append(safe)
            local_fields = {**fields, 'merchant': fields['merchant'] or context.get('local_merchant')}
            local_rows.append({**row, 'semantic_candidate': local_fields, 'additional_purpose_text': context['local_purpose'],
                'context_status': context['status'], 'context_reason': context['reason'],
                'privacy_rejected': context['privacy_rejected'],
                'diff': {'merchant': {'before': row['public_merchant_hint'], 'after': local_fields['merchant']},
                         'purpose': {'before': None, 'after': fields['purpose']}},
                'owner_field_reviewed': False, 'holdout_reviewed': False})
        old_unknown = {r['item_id'] for r in dataset['rows'] if r['public_merchant_hint'] is None}
        counts = Counter(r['context_reason'] for r in local_rows)
        coverage = {'observations': len(local_rows), 'previous_hints': len(local_rows) - len(old_unknown),
            'previously_without_hint': len(old_unknown),
            'newly_interpretable_candidates': sum(r['item_id'] in old_unknown and r['context_status'] in {'SAFE_CANDIDATE', 'LOCAL_CANDIDATE'} for r in local_rows),
            'new_allowlisted_candidates': sum(r['item_id'] in old_unknown and r['context_status'] == 'SAFE_CANDIDATE' for r in local_rows),
            'new_local_candidates': sum(r['item_id'] in old_unknown and r['context_status'] == 'LOCAL_CANDIDATE' for r in local_rows),
            'local_business_candidates_requiring_attestation': sum(r['context_status'] == 'LOCAL_CANDIDATE' for r in local_rows),
            'safe_candidate_records': sum(r['context_status'] == 'SAFE_CANDIDATE' for r in local_rows),
            'privacy_rejected_records': sum(r['privacy_rejected'] for r in local_rows),
            'still_unknown_records': sum(r['context_status'] not in {'SAFE_CANDIDATE', 'LOCAL_CANDIDATE'} for r in local_rows),
            'needs_owner_local_review': sum(r['context_status'] == 'NEEDS_OWNER_LOCAL_REVIEW' for r in local_rows),
            'reason_counts': dict(counts), 'owner_reviewed_fields': 0}
        groups: dict[str, list[str]] = {}
        after_cutoff: set[str] = set()
        policy = HumanLabels(frozen).policy()
        assert policy is not None
        for row in local_rows:
            merchant = row['semantic_candidate']['merchant']
            if merchant:
                group = digest([CONTEXT_VERSION, merchant])
                groups.setdefault(group, []).append(row['item_id'])
                if row['observed_date'] >= policy['cutoff']:
                    after_cutoff.add(group)
        # A proposal is not owner consent or a verified independent test set.
        eligible = sorted(after_cutoff)
        suggested = eligible[:max(1, len(eligible) // 5)]
        binding = digest([CONTEXT_VERSION, dataset['dataset_binding'], local_rows, EXTRA_MERCHANTS, PURPOSES])
        common = {**dataset, 'context_version': CONTEXT_VERSION, 'preview_binding': binding,
            'coverage': coverage, 'export_ready': False, 'upload_authorized': False,
            'warning': 'NOT ANONYMOUS. Local preview only; no upload authority. Per-field owner review required.',
            'holdout': {'status': 'AWAITING_HUMAN_REVIEW', 'human_labels': dataset['human_labels'],
                        'training_started': False, 'scoring_available': False,
                        'groups': groups, 'suggested_groups': suggested,
                        'merchant_entity_independence_verified': False,
                        'instruction': 'Reserve human-reviewed temporal/merchant holdout before independent AI annotation; do not tune or train on it.'}}
        return ({**common, 'variant': 'public_safe_context_preview', 'rows': safe_rows},
                {**common, 'variant': 'STRICTLY_OWNER_LOCAL', 'contains_private_fields': True, 'rows': local_rows})

    def preview(self, *, private: bool = False) -> dict[str, Any]:
        with review_snapshot(self.store) as frozen:
            public, local = self._build(frozen)
            return local if private else public

    def export_reviewed(self, selection: ContextExportRequest) -> dict[str, Any]:
        if selection.confirmed is not True or not selection.actor.strip():
            raise ValueError('Explicit owner field review required')
        with review_snapshot(self.store) as frozen:
            public, local = self._build(frozen)
        if selection.preview_binding != public['preview_binding']:
            raise ValueError('Context/source/label changed; preview again')
        rows = {r['item_id']: r for r in local['rows']}
        groups = local['holdout']['groups']
        if len(selection.holdout_groups) != len(set(selection.holdout_groups)) or any(g not in groups for g in selection.holdout_groups):
            raise ValueError('Unknown or duplicated holdout group')
        heldout = {item for group in selection.holdout_groups for item in groups[group]}
        approved: dict[tuple[str, str], str] = {}
        for field in selection.fields:
            row = rows.get(field.item_id)
            key = (field.item_id, field.field)
            if (row is None or field.item_id in heldout or key in approved or row['source_binding'] != field.source_binding
                    or row['context_status'] not in {'SAFE_CANDIDATE', 'LOCAL_CANDIDATE'}
                    or row['semantic_candidate'][field.field] != field.value):
                raise ValueError('Unreviewable, duplicate or changed semantic field')
            approved[key] = field.value
        for row in public['rows']:
            row['semantic_candidate'] = {name: approved.get((row['item_id'], name)) for name in ('merchant', 'purpose')}
            row['owner_field_reviewed'] = any(row['semantic_candidate'].values())
            if row['owner_field_reviewed']:
                row['context_status'] = 'OWNER_REVIEWED_NONPERSONAL_FIELD'
            if row['context_status'] == 'SAFE_CANDIDATE' and not row['owner_field_reviewed']:
                row['context_status'] = 'AWAITING_OWNER_FIELD_REVIEW'
            if row['item_id'] in heldout:
                row['context_status'] = 'HOLDOUT_EXCLUDED_FROM_AI'
                row['confirmed_category'] = None
        public.update(variant='public_safe_context_reviewed', export_ready=True,
                      owner_review={'actor_sha256': digest(selection.actor), 'receipt_sha256': digest(selection.model_dump(mode='json')),
                                    'reviewed_fields': len(approved), 'human_labels_created': 0})
        public['coverage']['owner_reviewed_fields'] = len(approved)
        public['holdout'].update(status='RESERVED_AWAITING_HUMAN_LABELS' if heldout else 'NOT_RESERVED',
                                 reserved_items=len(heldout), owner_confirmed_groups=selection.holdout_groups)
        public['instructions'] += ' Use only explicitly owner-reviewed semantic_candidate fields. No economic intent from transfers/amount/direction alone.'
        public['instructions'] += ' HOLDOUT_EXCLUDED_FROM_AI rows must receive only abstention; do not predict their category.'
        return public
