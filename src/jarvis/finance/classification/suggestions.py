"""Local suggestions on frozen review evidence; never financial or label apply."""
from __future__ import annotations

import hashlib
import marshal
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from jarvis.finance.classification.layers import l5_merchant_kb
from jarvis.finance.classification.taxonomy import OUTGOING_CLASSES
from jarvis.finance.event_taxonomy import (
    ALIASES,
    NON_ECONOMIC_CATEGORIES,
    TAXONOMY_VERSION,
    canonical_category,
)
from jarvis.finance.ml_local.merchant_kb import MERCHANT_KB
from jarvis.finance.review_backup import review_snapshot
from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_store import ReviewStore, encoded, source_ordinal

_RULE_CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

def rules_revision() -> str:
    implementations = [hashlib.sha256(marshal.dumps(fn.__code__.replace(co_filename="", co_firstlineno=0))).hexdigest() for fn in (l5_merchant_kb, canonical_category)]
    entries = l5_merchant_kb.__globals__.get("MERCHANT_KB", MERCHANT_KB)
    aliases = canonical_category.__globals__.get("ALIASES", ALIASES)
    return hashlib.sha256(encoded([_RULE_CODE_HASH, implementations, TAXONOMY_VERSION, aliases, OUTGOING_CLASSES, [asdict(row) for row in entries]]).encode()).hexdigest()

Reason = Literal["merchant_rule", "human_feedback", "conflicting_human_feedback", "local_model", "source_conflict", "unknown_economic_direction", "invalid_record", "no_match", "invalid_model_output"]
ModelStatus = Literal["not_needed", "unavailable", "abstained", "matched"]


class LocalModel(Protocol):
    @property
    def is_available(self) -> bool: ...
    def predict(self, text: str) -> dict[str, Any] | None: ...


class CategorySuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ordinal: int
    parser_version: str = Field(min_length=1, max_length=200)
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    rules_revision: str
    model_revision: str | None
    model_verification: Literal["unavailable", "synthetic_fixture", "unverified_artifact"] = "unavailable"
    taxonomy_version: str = TAXONOMY_VERSION
    status: Literal["suggestion", "pending_review"]
    suggested_category: str | None
    reason_code: Reason
    local_model_status: ModelStatus
    needs_review: Literal[True] = True
    human_confirmed: Literal[False] = False
    financial_apply_allowed: Literal[False] = False
    paid_calls: Literal[0] = 0
    feedback_evidence: tuple[dict[str, Any], ...] = ()
    feedback_count: int = Field(default=0, ge=0)


class LocalSuggestions:
    """Only trusted local model injection; no teacher, arbitrary path or cloud call."""
    def __init__(self, store: ReviewStore, *, model: LocalModel | None = None, model_revision: str | None = None,
                 synthetic_model: bool = False) -> None:
        if model is not None and (model_revision is None or len(model_revision) != 64 or any(c not in "0123456789abcdef" for c in model_revision)):
            raise ValueError("Explicit local model artifact revision required")
        self.model_revision = model_revision
        self.store, self.model = store, model
        self.model_verification: Literal["unavailable", "synthetic_fixture", "unverified_artifact"] = "unavailable"
        self.model_evidence: dict[str, Any] | None = None
        if model is not None:
            from jarvis.finance.ml_local.local_categorizer import LocalCategorizer
            if isinstance(model, LocalCategorizer):
                self.model_evidence = model.artifact_evidence(model_revision)
                self.model_revision = self.model_evidence.get("artifact_sha256")
                self.model_verification = "unverified_artifact"
                self.model = None  # observed hashes are not source trust or compatible inference
            elif synthetic_model:
                self.model_verification = "synthetic_fixture"
            else:
                self.model = None  # a caller string/arbitrary callback cannot assert a genuine model
        self._cache: dict[str, CategorySuggestion] = {}

    def preview(self, item_id: str) -> CategorySuggestion:
        return self.preview_items([item_id])[0]

    def preview_items(self, item_ids: list[str]) -> list[CategorySuggestion]:
        if not 1 <= len(item_ids) <= 20 or len(set(item_ids)) != len(item_ids):
            raise ValueError("Distinct bounded review IDs required")
        with review_snapshot(self.store) as frozen:
            context = self._context(frozen)
            return [self._preview_frozen(frozen, item_id, _context=context) for item_id in item_ids]

    @staticmethod
    def _context(frozen: ReviewStore) -> dict[str, Any]:
        """Reuse one verified immutable snapshot for a bounded local review."""
        meta = frozen.metadata()
        audit = HumanLabels(frozen).audit()
        binding = {'metadata': meta, 'review_audit': frozen.audit(), 'label_audit': audit, 'rules_revision': rules_revision()}
        return {'metadata': meta, 'revision': hashlib.sha256(encoded(binding).encode()).hexdigest(),
                'items': {i['id']: i for i in frozen.items()}, 'labels': {e['item_id']: e for e in audit}}

    @staticmethod
    def _feedback(frozen: ReviewStore, row: dict[str, Any], *, _context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Reuse explicit labels only for identical normalized local expense context."""
        def signature(operation: dict[str, Any]) -> tuple[str, str, str] | None:
            merchant, text, currency = (operation.get(name) for name in ("merchant", "description", "currency"))
            if not isinstance(merchant, str) or not merchant.strip() or not isinstance(text, str):
                return None
            if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha() or not currency.isupper():
                return None
            try:
                amount = Decimal(str(operation["amount"]))
            except (KeyError, InvalidOperation):
                return None
            if not amount.is_finite() or amount >= 0:
                return None
            return (" ".join(merchant.casefold().split()), " ".join(text.casefold().split()), currency)
        key = signature(row)
        if key is None:
            return []
        items = _context['items'] if _context is not None else {item["id"]: item for item in frozen.items()}
        result = []
        for label in (_context['labels'] if _context is not None else HumanLabels(frozen).latest()).values():
            item = items.get(label["item_id"])
            if (label["action"] == "confirm" and item is not None and item.get("status") != "reject" and not item.get("conflict_reasons")
                    and item.get("source_kind") != "generated_pdf" and label["source_sha256"] == item["source_sha256"]
                    and signature(item["operation"]) == key):
                result.append({"item_id": label["item_id"], "source_sha256": label["source_sha256"],
                    "label_revision": label["seq"], "policy_hash": label["policy_hash"], "category": canonical_category(label["category"])})
        return result

    def _preview_frozen(self, frozen: ReviewStore, item_id: str, *, _context: dict[str, Any] | None = None) -> CategorySuggestion:
        context = _context if _context is not None else self._context(frozen)
        meta, revision = context['metadata'], context['revision']
        item = context['items'].get(item_id)
        if item is None:
            raise ValueError("Unknown review item")
        key = hashlib.sha256(encoded([revision, item, TAXONOMY_VERSION, self.model_revision, self.model_verification]).encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        category: str | None = None
        feedback: list[dict[str, Any]] = []
        reason: Reason = "no_match"
        model_status: ModelStatus = "not_needed"
        row = item["operation"]
        try:
            amount = Decimal(str(row["amount"]))
            text = row.get("description", "")
            merchant = row.get("merchant", "")
            valid = amount.is_finite() and isinstance(text, str) and isinstance(merchant, str) and len(text) <= 2000 and len(merchant) <= 200
        except (KeyError, InvalidOperation):
            amount, text, merchant, valid = Decimal(0), "", "", False
        if not valid:
            reason = "invalid_record"
        elif item.get("conflict_reasons") or item.get("source_kind") == "generated_pdf":
            reason = "source_conflict"
        elif amount >= 0 or any(word in text.casefold() for word in ("перевод", "возврат", "брокер", "transfer", "refund")):
            reason = "unknown_economic_direction"
        else:
            rule = l5_merchant_kb({"merchant": " ".join(merchant.casefold().split()), "description_clean": " ".join(text.casefold().split())})
            allowed = {canonical_category(c) for top, entry in OUTGOING_CLASSES.items() for c in [top, *entry["aliases"]]} - NON_ECONOMIC_CATEGORIES - {"unknown", "other", "investment", "investment_income", "dividend", "coupon"}
            feedback = self._feedback(frozen, row, _context=context)
            feedback_categories = {e["category"] for e in feedback}
            if feedback_categories:
                if len(feedback_categories) == 1 and feedback_categories <= allowed:
                    category, reason = next(iter(feedback_categories)), "human_feedback"
                else:
                    reason = "conflicting_human_feedback"
            elif rule and canonical_category(rule.category) in allowed:
                category, reason = canonical_category(rule.category), "merchant_rule"
            else:
                model_status = "unavailable"
                if self.model is not None:
                    try:
                        if self.model.is_available:
                            prediction = self.model.predict(text)
                            model_status = "abstained"
                            if prediction:
                                candidate = canonical_category(str(prediction.get("category", "")))
                                confidence = Decimal(str(prediction.get("confidence", "NaN")))
                                if candidate in allowed and confidence.is_finite() and Decimal("0.70") <= confidence <= 1:
                                    category, reason, model_status = candidate, "local_model", "matched"
                                else:
                                    reason = "invalid_model_output"
                    except Exception:  # noqa: BLE001 - optional local model, no raw errors
                        model_status = "unavailable"
        result = CategorySuggestion(item_id=item_id, source_sha256=item["source_sha256"], source_ordinal=source_ordinal(item),
            parser_version=meta["parser_version"], source_revision=revision, rules_revision=rules_revision(), model_revision=self.model_revision,
            model_verification=self.model_verification, status="suggestion" if category else "pending_review",
            suggested_category=category, reason_code=reason, local_model_status=model_status,
            feedback_evidence=tuple(feedback[:20]), feedback_count=len(feedback))
        if len(self._cache) >= 128:
            self._cache.clear()
        self._cache[key] = result
        return result
