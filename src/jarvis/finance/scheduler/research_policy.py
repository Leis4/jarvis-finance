"""Durable research capabilities on the existing private staging journal."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.core.config import settings
from jarvis.finance.db import FinanceDB
from jarvis.finance.review_store import encoded
from jarvis.knowledge.public_sources import QUESTION, checked_url


class ResearchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    purpose: Literal["public_research"] = "public_research"
    mode: Literal["mock", "live"] = "mock"
    research_type: Literal["public_explanation"] = "public_explanation"
    topics: tuple[str, ...] = (QUESTION,)
    urls: tuple[str, ...]
    domains: tuple[str, ...]
    expires_at: datetime
    tools: tuple[str, ...] = ("public_candidates", "read_public_sources", "explain_public_sources", "prepare_report")
    models: tuple[Literal["qwen3.7-flash"], ...] = ("qwen3.7-flash",)
    free_http: bool = Field(default=True, strict=True)
    paid_llm: bool = Field(default=False, strict=True)
    paid_search: Literal[False] = False
    maximum_tasks: int = Field(default=1, ge=1, le=20, strict=True)
    maximum_steps: int = Field(default=5, ge=1, le=5, strict=True)
    maximum_network_requests: int = Field(default=9, ge=1, le=40, strict=True)
    maximum_model_calls: int = Field(default=1, ge=0, le=1, strict=True)
    request_budget_rub: Decimal = Field(default=Decimal("0.03"), gt=0, le=Decimal("0.03"), allow_inf_nan=False)
    daily_budget_rub: Decimal = Field(default=Decimal("0.03"), gt=0, allow_inf_nan=False)
    monthly_budget_rub: Decimal = Field(default=Decimal("0.03"), gt=0, allow_inf_nan=False)
    search_endpoint: str | None = Field(default=None, max_length=500)

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Aware policy expiry required")
        return value.astimezone(UTC)

    @field_validator("topics")
    @classmethod
    def public_topics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re
        if not 1 <= len(value) <= 5 or any(not 1 <= len(topic) <= 300 or
                re.search(r"(?i)\b(pin|otp|cookie|password|my|mine|your|пароль|мой|моя|мои|мне|меня)\b|sk-|\d{4}", topic) for topic in value):
            raise ValueError("Bounded public topics required")
        return value


def policy_document(policy: ResearchPolicy) -> dict[str, Any]:
    """Keep pre-discovery grant hashes byte-compatible with their signed schema."""
    return policy.model_dump(mode="json", exclude={"search_endpoint"} if policy.search_endpoint is None else set())


class ResearchPolicies:
    def __init__(self, journal: FinanceDB) -> None:
        if journal.get_setting("agent_runtime_scope") != "private_staging":
            raise PermissionError("Private staging policy journal required")
        with journal.connect() as conn:
            if conn.execute("SELECT count(*) FROM transactions").fetchone()[0]:
                raise PermissionError("Financial journal refused")
        self.journal = journal

    def grant(self, policy: ResearchPolicy, *, owner_action: bool = False) -> str:
        if policy.mode == "live" and (not owner_action or os.environ.get("CODEX_THREAD_ID") or os.environ.get("PYTEST_CURRENT_TEST")):
            raise PermissionError("Separate genuine owner action required")
        if (policy.expires_at <= datetime.now(UTC) or not (2 <= len(policy.urls) <= 3 or (policy.search_endpoint and not policy.urls))
                or policy.daily_budget_rub > Decimal(str(settings.daily_budget_rub))
                or policy.monthly_budget_rub > Decimal(str(settings.monthly_budget_rub))
                or policy.request_budget_rub > Decimal(str(settings.request_ceiling_rub))
                or any(tool not in {"public_candidates", "read_public_sources", "explain_public_sources", "prepare_report"} for tool in policy.tools)):
            raise PermissionError("Policy scope/budget refused")
        for url in policy.urls:
            checked_url(url, frozenset(policy.domains))
        if policy.search_endpoint:
            from jarvis.knowledge.discovery import search_endpoint
            search_endpoint(policy.search_endpoint)
            if not policy.domains or not policy.free_http:
                raise PermissionError("Search requires explicit public domains and free HTTP scope")
        ident = uuid4().hex
        with self.journal.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            data = {"policy": policy_document(policy), "identity_version": 2, "revoked": False,
                    "tasks": [], "network": 0, "models": 0, "effects": [], "steps": {}}
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("research_policy:" + ident, encoded(data)))
            self.journal.audit(conn, "research_policy_granted", {"policy_id": ident, "mode": policy.mode,
                "identity_version": 2,
                "policy_sha256": hashlib.sha256(encoded(policy_document(policy)).encode()).hexdigest()})
        return ident

    def check(self, ident: str, *, synthetic: bool, now: datetime | None = None) -> ResearchPolicy:
        data = self.journal.get_setting("research_policy:" + ident)
        if not data:
            raise PermissionError("Research permission unavailable")
        policy = ResearchPolicy.model_validate(data["policy"])
        with self.journal.connect() as conn:
            grant = conn.execute("SELECT payload FROM audit_log WHERE event_type='research_policy_granted' AND json_extract(payload,'$.policy_id')=? ORDER BY id DESC LIMIT 1", (ident,)).fetchone()
            revoked = conn.execute("SELECT 1 FROM audit_log WHERE event_type='research_policy_revoked' AND json_extract(payload,'$.policy_id')=? LIMIT 1", (ident,)).fetchone()
        expected = hashlib.sha256(encoded(policy_document(policy)).encode()).hexdigest()
        if grant is None or json.loads(grant[0]).get("policy_sha256") != expected:
            raise PermissionError("Immutable policy grant mismatch")
        if data.get("identity_version", 1) != json.loads(grant[0]).get("identity_version", 1):
            raise PermissionError("Immutable identity version mismatch")
        if revoked or data["revoked"] or policy.expires_at <= (now or datetime.now(UTC)) or synthetic != (policy.mode == "mock"):
            raise PermissionError("Research permission revoked, expired or wrong transport")
        return policy

    def planner_identity(self, ident: str, task_id: int, *, event_identity: str, model: str,
                         synthetic: bool, question: str = QUESTION) -> dict[str, str]:
        """Bind one durable invocation to an authenticated grant and persisted task."""
        policy = self.check(ident, synthetic=synthetic)
        if model not in policy.models or not policy.paid_llm or question not in policy.topics:
            raise PermissionError("Planner identity scope refused")
        with self.journal.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            data = json.loads(conn.execute("SELECT value FROM settings WHERE key=?", ("research_policy:" + ident,)).fetchone()[0])
            row = conn.execute("SELECT name,params FROM scheduled_tasks WHERE id=? AND task_type='agent_core'", (task_id,)).fetchone()
            if row is None or data["policy"] != policy_document(policy) or data["revoked"] or policy.expires_at <= datetime.now(UTC):
                raise PermissionError("Planner task/grant unavailable")
            task = json.loads(row[1])
            if (task.get("research_policy_id") != ident or task.get("intent") != policy.purpose
                    or event_identity != str(task_id) + task.get("input_revision", "")):
                raise PermissionError("Persisted planner task identity mismatch")
            version = data.get("identity_version", 1)
            if version == 1:
                return {"version": "public-research-plan-v1", "fingerprint": hashlib.sha256(("public-research-plan-v1:" + event_identity + model).encode()).hexdigest()}
            if version != 2:
                raise PermissionError("Unknown planner identity version")
            material = {"version": "public-research-plan-v2", "grant": ident,
                "policy_sha256": hashlib.sha256(encoded(policy_document(policy)).encode()).hexdigest(),
                "task": row[0], "task_id": task_id, "revision": task["input_revision"],
                "purpose": policy.purpose, "model": model, "topic_sha256": hashlib.sha256(question.encode()).hexdigest()}
            binding = {"version": material["version"], "fingerprint": hashlib.sha256(encoded(material).encode()).hexdigest()}
            key = f"research_invocation:{ident}:{task_id}"
            old = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            audit = conn.execute("SELECT payload FROM audit_log WHERE event_type='research_invocation_bound' AND json_extract(payload,'$.key')=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            if old is not None:
                if json.loads(old[0]) != binding or audit is None or json.loads(audit[0]).get("binding") != binding:
                    raise PermissionError("Immutable invocation mismatch")
            else:
                if audit is not None or data["models"] or f"{task_id}:model:{model}" in data["effects"]:
                    raise PermissionError("Missing invocation lineage; no new paid identity")
                conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, encoded(binding)))
                self.journal.audit(conn, "research_invocation_bound", {"key": key, "binding": binding})
            return binding

    def revoke(self, ident: str) -> None:
        with self.journal.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM settings WHERE key=?", ("research_policy:" + ident,)).fetchone()
            if row is None:
                raise PermissionError("Unknown policy")
            data = json.loads(row[0]); data["revoked"] = True
            conn.execute("UPDATE settings SET value=? WHERE key=?", (encoded(data), "research_policy:" + ident))
            self.journal.audit(conn, "research_policy_revoked", {"policy_id": ident})

    def consume(self, ident: str, task_id: int, kind: Literal["task", "step", "http", "model", "key_check", "discovery"], effect: str,
                *, synthetic: bool, question: str = QUESTION, now: datetime | None = None) -> ResearchPolicy:
        policy = self.check(ident, synthetic=synthetic, now=now)
        reason = None
        with self.journal.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            key = "research_policy:" + ident
            data = json.loads(conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0])
            token = f"{task_id}:{kind}:{effect}"
            if data["policy"] != policy_document(policy) or data["revoked"] or policy.expires_at <= (now or datetime.now(UTC)):
                reason = "Permission revoked/expired"
            elif question not in policy.topics:
                reason = "Topic refused"
            elif kind == "task":
                if task_id not in data["tasks"]:
                    if len(data["tasks"]) >= policy.maximum_tasks:
                        reason = "Task cap"
                    else:
                        data["tasks"].append(task_id)
            elif task_id not in data["tasks"] or token in data["effects"]:
                reason = "Unclaimed task or replayed effect"
            elif kind == "step":
                count = data["steps"].get(str(task_id), 0)
                if effect not in (*policy.tools, "planner") or count >= policy.maximum_steps:
                    reason = "Step/tool cap"
                else:
                    data["steps"][str(task_id)] = count + 1
            elif data["network"] >= policy.maximum_network_requests:
                reason = "Network cap"
            elif kind == "discovery" and (not policy.free_http or not policy.search_endpoint or effect != policy.search_endpoint):
                reason = "Discovery scope refused"
            elif kind == "http" and (not policy.free_http or (not policy.search_endpoint and effect not in policy.urls)):
                reason = "HTTP scope refused"
            elif kind == "http" and policy.search_endpoint:
                try:
                    checked_url(effect, frozenset(policy.domains))
                except ValueError:
                    reason = "Discovered HTTP domain refused"
                if reason is None:
                    data["network"] += 1
            elif kind == "model" and (not policy.paid_llm or effect not in policy.models or data["models"] >= policy.maximum_model_calls):
                reason = "Model scope/cap refused"
            elif kind == "key_check" and (not policy.paid_llm or effect != "aitunnel_key_status"):
                reason = "Key check scope refused"
            else:
                data["network"] += 1
                if kind == "model":
                    data["models"] += 1
            if reason is None:
                if kind != "task":
                    data["effects"].append(token)
                conn.execute("UPDATE settings SET value=? WHERE key=?", (encoded(data), key))
            self.journal.audit(conn, "research_permission_refused" if reason else "research_permission_consumed",
                {"policy_id": ident, "task_id": task_id, "kind": kind, "reason": reason})
        if reason:
            raise PermissionError(reason)
        return policy
