"""Explicit private-field permission and proposed categories, never human labels."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_store import ReviewStore, encoded


def normalize_teacher_merchant(value: str) -> str:
    """Remove only an anchored, recognized bank card-operation suffix."""
    value = " ".join(value.split())
    return re.sub(r"(?i)[.\s]+операция\s+по\s+карте\s+(?:[*•x]\s*){2,}\d{4}[.\s]*$", "", value).strip()


class TeacherPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    purpose: Literal["private_category_proposal", "public_development"] = "private_category_proposal"
    mode: Literal["mock", "live"]
    fields: tuple[Literal["merchant", "description"], ...]
    model: Literal["qwen3.7-flash"] = "qwen3.7-flash"
    expires_at: datetime
    maximum_requests: int = Field(default=1, ge=1, le=20, strict=True)
    request_budget_rub: Decimal = Field(default=Decimal("0.03"), gt=0, le=Decimal("0.03"), allow_inf_nan=False)
    daily_budget_rub: Decimal = Field(default=Decimal("0.03"), gt=0, le=Decimal(1), allow_inf_nan=False)
    monthly_budget_rub: Decimal = Field(default=Decimal("0.03"), gt=0, le=Decimal(10), allow_inf_nan=False)

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Aware permission expiry required")
        return value.astimezone(UTC)

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Explicit distinct fields required")
        return values

    @model_validator(mode="after")
    def purpose_fields(self) -> TeacherPolicy:
        if (self.purpose == "public_development") != (not self.fields):
            raise ValueError("Public development sends no private fields; teacher requires explicit fields")
        return self


# Fixed public exercises: no caller text, financial records or tool authority.
PUBLIC_EXERCISES = {
    "public-smoke": {"merchant": "Синтетический продуктовый магазин: хлеб"},
    "public-transport": {"merchant": "Синтетический общественный транспорт: билет"},
    "public-abstention": {"merchant": "Неизвестный синтетический объект, назначение не указано"},
}


class CategoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    category: str | None = Field(max_length=100)
    basis: Literal["merchant_identity", "description_keyword", "insufficient_data"]


class TeacherPermissions:
    """Reuse private ReviewStore metadata and its append-only audit chain."""
    def __init__(self, store: ReviewStore) -> None:
        self.store = store

    def grant(self, policy: TeacherPolicy, *, confirmed: bool, actor: str, request_id: str | None = None) -> str:
        now = datetime.now(UTC)
        if not confirmed or not actor.strip() or len(actor) > 100:
            raise PermissionError("Explicit owner confirmation required")
        if policy.mode == "live" and (os.environ.get("CODEX_THREAD_ID") or os.environ.get("PYTEST_CURRENT_TEST")):
            raise PermissionError("Separate owner live permission required")
        if not now < policy.expires_at <= now + timedelta(days=7):
            raise PermissionError("Permission expiry limited to seven days")
        if request_id is not None and not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise PermissionError("Invalid owner action identity")
        labels = HumanLabels(self.store)
        label_policy = labels.policy()
        if policy.purpose == "private_category_proposal" and (label_policy is None or not 1 <= len(label_policy["categories"]) <= 30):
            raise PermissionError("Bounded frozen human category policy required")
        ident = uuid4().hex
        payload = encoded({"policy": policy.model_dump(mode="json"),
                           "label_policy_hash": self.store.metadata()["label_policy_hash"] if policy.purpose == "private_category_proposal" else None})
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events = self.store._audit(conn)
            if request_id is not None:
                action_hash = hashlib.sha256(encoded([payload, actor]).encode()).hexdigest()
                old = conn.execute("SELECT value FROM review_meta WHERE key=?", ("teacher_activation:" + request_id,)).fetchone()
                if old:
                    recorded = json.loads(old[0])
                    if recorded["sha256"] != action_hash:
                        raise PermissionError("Owner action identity already bound to another scope")
                    self._check(conn, recorded["permission_id"], synthetic=policy.mode == "mock")
                    return str(recorded["permission_id"])
                conn.execute("INSERT INTO review_meta VALUES (?,?)", ("teacher_activation:" + request_id,
                    encoded({"sha256": action_hash, "permission_id": ident})))
            conn.execute("INSERT INTO review_meta VALUES (?,?)", ("teacher_policy:" + ident, payload))
            self.store._append_event(conn, {"seq": len(events) + 1, "item_id": "teacher_policy:" + ident,
                "domain": "private_teacher", "action": "grant_teacher", "actor": actor,
                "policy_sha256": hashlib.sha256(payload.encode()).hexdigest(), "timestamp": now.isoformat()})
            conn.commit()
        return ident

    def check(self, ident: str, *, synthetic: bool) -> TeacherPolicy:
        with self.store.connect() as conn:
            return self._check(conn, ident, synthetic=synthetic)

    def _check(self, conn: Any, ident: str, *, synthetic: bool) -> TeacherPolicy:
        if not re.fullmatch(r"[0-9a-f]{32}", ident):
            raise PermissionError("Unknown teacher permission")
        events = self.store._audit(conn)
        row = conn.execute("SELECT value FROM review_meta WHERE key=?", ("teacher_policy:" + ident,)).fetchone()
        grants = [e for e in events if e.get("item_id") == "teacher_policy:" + ident]
        if not row or not grants or grants[0].get("action") != "grant_teacher" or any(e.get("action") == "revoke_teacher" for e in grants):
            raise PermissionError("Teacher permission unavailable or revoked")
        if hashlib.sha256(row[0].encode()).hexdigest() != grants[0].get("policy_sha256"):
            raise PermissionError("Teacher permission integrity refused")
        content = json.loads(row[0])
        policy = TeacherPolicy.model_validate(content["policy"])
        current_hash = conn.execute("SELECT value FROM review_meta WHERE key='label_policy_hash'").fetchone()
        if policy.purpose == "private_category_proposal" and (not current_hash or current_hash[0] != content["label_policy_hash"]):
            raise PermissionError("Category policy changed")
        if policy.expires_at <= datetime.now(UTC) or synthetic != (policy.mode == "mock"):
            raise PermissionError("Teacher expiry/transport refused")
        return policy

    def revoke(self, ident: str, *, actor: str) -> None:
        if not actor.strip() or len(actor) > 100:
            raise PermissionError("Explicit revocation actor required")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            events = self.store._audit(conn)
            matching = [e for e in events if e.get("item_id") == "teacher_policy:" + ident]
            if not matching:
                raise PermissionError("Unknown teacher permission")
            if any(e.get("action") == "revoke_teacher" for e in matching):
                return
            self.store._append_event(conn, {"seq": len(events) + 1, "item_id": "teacher_policy:" + ident,
                "domain": "private_teacher", "action": "revoke_teacher", "actor": actor,
                "timestamp": datetime.now(UTC).isoformat()})
            conn.commit()

    def consume(self, ident: str, request: str, *, synthetic: bool) -> None:
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policy = self._check(conn, ident, synthetic=synthetic)
            events = self.store._audit(conn)
            attempts = [e for e in events if e.get("permission_id") == ident and e.get("action") == "attempt_teacher"]
            if any(e.get("request_sha256") == request for e in attempts) or len(attempts) >= policy.maximum_requests:
                raise PermissionError("Teacher request consumed; no automatic resend")
            self.store._append_event(conn, {"seq": len(events) + 1, "item_id": "teacher_request:" + request,
                "domain": "private_teacher", "action": "attempt_teacher", "permission_id": ident,
                "request_sha256": request, "timestamp": datetime.now(UTC).isoformat()})
            conn.commit()

    def evidence(self, ident: str, item_id: str, *, synthetic: bool) -> dict[str, Any]:
        from jarvis.finance.review_backup import review_snapshot
        with review_snapshot(self.store) as frozen:
            return TeacherPermissions(frozen)._evidence(ident, item_id, synthetic=synthetic)

    def _evidence(self, ident: str, item_id: str, *, synthetic: bool) -> dict[str, Any]:
        policy = self.check(ident, synthetic=synthetic)
        if policy.purpose == "public_development":
            if item_id not in PUBLIC_EXERCISES:
                raise PermissionError("Only fixed public development exercises allowed")
            fixture = {"fields": PUBLIC_EXERCISES[item_id], "categories": ["food", "transport"],
                       "parser_version": "public-development-v1", "item_id": item_id}
            digest = hashlib.sha256(encoded(fixture).encode()).hexdigest()
            return {**fixture, "source_sha256": digest, "binding": digest}
        entries = [i for i in self.store.items() if i["id"] == item_id]
        if (not entries or entries[0]["status"] == "reject" or entries[0].get("conflict_reasons")
                or entries[0].get("source_kind") == "generated_pdf"):
            raise PermissionError("Review source unavailable/conflicted")
        item = entries[0]
        fields: dict[str, str] = {}
        original_fields: dict[str, str] = {}
        for field in policy.fields:
            value = item["operation"].get(field)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 300:
                raise PermissionError("Selected private field requires safe bounded content")
            original_fields[field] = " ".join(value.split())
            value = normalize_teacher_merchant(value) if field == "merchant" else value
            if (not value or re.search(r"(?i)\b(?:pin|otp|cookie|password|authorization|пароль|пин)\b|sk-|(?:\d[ -]?){12,}|@|https?://|(?:[*•xX]\s*){2,}\d{4}", value)):
                raise PermissionError("Selected private field requires safe bounded content")
            fields[field] = " ".join(value.split())
        categories = HumanLabels(self.store).policy()
        assert categories is not None
        # Preserve historical request identity; normalization is a transport projection.
        binding = {"source_sha256": item["source_sha256"], "item_id": item_id,
                   "packet_hash": self.store.metadata()["packet_hash"],
                   "parser_version": self.store.metadata()["parser_version"],
                   "label_revision": HumanLabels(self.store).latest().get(item_id, {}).get("seq", 0),
                   "categories": categories["categories"], "fields": original_fields}
        return {"fields": fields, "categories": categories["categories"],
                "parser_version": binding["parser_version"], "source_sha256": item["source_sha256"],
                "binding": hashlib.sha256(encoded(binding).encode()).hexdigest()}

    def recorded_result(self, ident: str, request: str) -> dict[str, Any] | None:
        """Read audited historical outcome without presenting a new proposal."""
        key = "teacher_result:" + request
        with self.store.connect() as conn:
            events = self.store._audit(conn)
            row = conn.execute("SELECT value FROM review_meta WHERE key=?", (key,)).fetchone()
            matching = [e for e in events if e.get("item_id") == key and e.get("permission_id") == ident]
            if row is None:
                return None
            if not matching or hashlib.sha256(row[0].encode()).hexdigest() != matching[-1].get("result_sha256"):
                raise PermissionError("Historical result integrity refused")
            return dict(json.loads(row[0]))

    def record_result(self, ident: str, request: str, result: dict[str, Any]) -> None:
        """Retain a proposal in the existing private store, never label_events."""
        payload = encoded(result)
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check(conn, ident, synthetic=result["simulation"])
            events = self.store._audit(conn)
            conn.execute("INSERT INTO review_meta VALUES (?,?)", ("teacher_result:" + request, payload))
            self.store._append_event(conn, {"seq": len(events) + 1, "item_id": "teacher_result:" + request,
                "domain": "private_teacher", "action": "propose_category", "permission_id": ident,
                "result_sha256": hashlib.sha256(payload.encode()).hexdigest(), "timestamp": datetime.now(UTC).isoformat()})
            conn.commit()
