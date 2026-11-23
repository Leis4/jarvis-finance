"""Bounded private agent execution on the existing scheduled task journal."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.core.llm.budget_store import BudgetReservationDenied, BudgetStoreUnavailable
from jarvis.core.llm.errors import LLMError
from jarvis.finance.audit_report import financial_question
from jarvis.finance.classification.suggestions import rules_revision
from jarvis.finance.db import FinanceDB
from jarvis.finance.insights.deterministic import (
    ENGINE_REVISION,
    InsightPolicy,
    financial_insights,
    quality_notice,
)
from jarvis.finance.review_labels import HumanLabels
from jarvis.finance.review_store import ReviewStore, encoded
from jarvis.finance.scheduler.research_policy import ResearchPolicies
from jarvis.knowledge.errors import KnowledgeUnavailable
from jarvis.knowledge.public_sources import PublicSources
from jarvis.services.assistant import AssistantService
from jarvis.services.chat_scenarios import ChatScenario, calculate_scenario

State = Literal["QUEUED", "RUNNING", "WAITING_FOR_DATA", "WAITING_FOR_APPROVAL", "COMPLETED", "FAILED", "UNKNOWN", "CANCELLED"]
Permission = Literal["AUTO_READ", "LOCAL_DERIVED_WRITE", "HUMAN_APPROVAL_REQUIRED", "DENIED"]


class AgentTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_analyst_permission_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    expected_analyst_binding: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    intent: Literal["review_overview", "financial_report", "financial_insights", "goal_scenario", "classification_preview", "workflow", "public_research", "financial_analysis", "local_analysis", "owner_financial_analysis"]
    origin: Literal["user", "scheduled", "event", "data_refresh", "research", "financial_analysis", "classification", "knowledge_processing", "report_generation", "human_review_required"]
    event_key: str = Field(pattern=r"^[a-zA-Z0-9:_-]{1,100}$")
    input_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_scope: tuple[Literal["review", "accepted_finance", "hypothetical", "public_research"], ...]
    allowed_capabilities: tuple[Literal["AUTO_READ", "LOCAL_DERIVED_WRITE"], ...] = ("AUTO_READ", "LOCAL_DERIVED_WRITE")
    priority: int = Field(default=3, ge=1, le=5, strict=True)
    deadline: datetime
    run_budget_rub: Literal["0", "0.03"] = "0"
    public_evidence_task: int | None = Field(default=None, ge=1, strict=True)
    research_policy_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    maximum_steps: int = Field(default=3, ge=1, le=5, strict=True)
    timeout_seconds: float = Field(default=5, gt=0, le=30, allow_inf_nan=False)
    month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    currency: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    scenario: ChatScenario | None = None
    batch_offset: int = Field(default=0, ge=0, le=1_000_000, strict=True)

    @field_validator("deadline")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Aware task deadline required")
        return value.astimezone(UTC)


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolRegistry:
    """Trusted in-process handlers; external input cannot register tools or paths."""
    def __init__(self) -> None:
        self._tools: dict[str, tuple[Permission, Callable[[ToolInput], Awaitable[dict[str, Any]]]]] = {}

    def register(self, name: str, permission: Permission, handler: Callable[[ToolInput], Awaitable[dict[str, Any]]]) -> None:
        if name in self._tools:
            raise ValueError("Duplicate tool identity")
        self._tools[name] = (permission, handler)

    def permission(self, name: str) -> Permission:
        if name not in self._tools:
            raise PermissionError("Unknown tool denied")
        return self._tools[name][0]

    async def call(self, name: str, inputs: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
        permission = self.permission(name)
        if permission not in allowed or permission in {"DENIED", "HUMAN_APPROVAL_REQUIRED"}:
            raise PermissionError("Tool capability denied")
        return await self._tools[name][1](ToolInput.model_validate(inputs))


class AgentRuntime:
    """No daemon, paid tool, financial apply, shell, dynamic path or MCP dispatch."""
    def __init__(self, journal: FinanceDB, store: ReviewStore, *, assistant: AssistantService | None = None,
                 public_sources: PublicSources | None = None,
                 research_planner: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
                 insight_policy: InsightPolicy | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not journal.path.is_file() or journal.path.is_symlink():
            raise ValueError("Existing explicit private staging journal required")
        if journal.get_setting("agent_runtime_scope") != "private_staging":
            raise ValueError("Production/unapproved journal refused")
        with journal.connect() as conn:
            if conn.execute("SELECT count(*) FROM transactions").fetchone()[0]:
                raise ValueError("Financial ledger cannot be the agent staging journal")
        self.journal, self.store, self.clock = journal, store, clock
        self.assistant = assistant or AssistantService(store)
        self.public_sources = public_sources
        self.research_planner = research_planner
        self.insight_policy = insight_policy or InsightPolicy()

    def revision(self) -> str:
        """Bind the queue/audit and any configured immutable ledger bytes."""
        # Derived projections and analyst receipts are outputs, not financial inputs.
        # Audit verification still covers every event, including excluded outputs.
        metadata = {k: v for k, v in self.store.metadata().items()
                    if not k.startswith(("analyst_result:", "statement_projection:"))}
        audit = [e for e in self.store.audit() if e.get("domain") != "statement_projection"
                 and not (e.get("domain") == "financial_analyst" and e.get("action") in {"attempt_analyst", "analyst_result"})]
        material: dict[str, Any] = {"metadata": metadata, "audit": audit,
                                    "label_audit": HumanLabels(self.store).audit(), "rules_revision": rules_revision()}
        if self.assistant.ledger is not None:
            material["ledger_sha256"] = hashlib.sha256(self.assistant.ledger.read_bytes()).hexdigest()
            material["accepted_scope"] = [self.assistant.accepted_sha256, sorted(self.assistant.accepted_sources)]
            material["insight_policy"] = self.insight_policy.model_dump(mode="json")
            material["insight_engine"] = ENGINE_REVISION
        if self.public_sources is not None:
            material["public_configuration"] = self.public_sources.candidates()
            material["public_planner_configured"] = self.research_planner is not None
        return hashlib.sha256(encoded(material).encode()).hexdigest()

    def _record(self, conn: Any, ident: int, state: State, data: dict[str, Any]) -> None:
        stamp = self.clock()
        if stamp.tzinfo is None:
            raise ValueError("Aware runtime clock required")
        payload = encoded(data)
        if len(payload.encode()) > 64_000:
            raise ValueError("Bounded task result required")
        conn.execute("INSERT INTO scheduled_task_runs(task_id,run_date,status,message,created_at) VALUES(?,?,?,?,?)",
            (ident, stamp.date().isoformat(), state, payload, stamp.isoformat()))
        self.journal.audit(conn, "agent_task_state", {"task_id": ident, "state": state, "payload_sha256": hashlib.sha256(payload.encode()).hexdigest()})

    def submit(self, task: AgentTask) -> int:
        payload = task.model_dump(mode="json", exclude_none=True)
        identity_fields = {key: payload[key] for key in ("origin", "event_key", "intent", "input_revision")}
        if task.expected_analyst_permission_id is not None:
            identity_fields['expected_analyst_permission_id'] = task.expected_analyst_permission_id
            identity_fields['expected_analyst_binding'] = task.expected_analyst_binding
        if task.public_evidence_task is not None:
            identity_fields["public_evidence_task"] = task.public_evidence_task
        if task.batch_offset:
            identity_fields["batch_offset"] = task.batch_offset
        identity = "agent:" + hashlib.sha256(encoded(identity_fields).encode()).hexdigest()
        with self.journal.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT id,params FROM scheduled_tasks WHERE name=? AND task_type='agent_core'", (identity,)).fetchone()
            if old:
                old_payload = AgentTask.model_validate_json(old[1]).model_dump(mode="json", exclude_none=True)
                if (task.intent in {"financial_insights", "financial_analysis", "owner_financial_analysis", "local_analysis"} or (task.intent == "public_research" and task.origin == "financial_analysis")) and {k: v for k, v in old_payload.items() if k != "deadline"} == {k: v for k, v in payload.items() if k != "deadline"}:
                    return int(old[0])  # replay never extends the first task's expiry
                if old_payload != payload:
                    raise ValueError("Conflicting stable task identity")
                return int(old[0])
            stamp = self.clock().isoformat()
            cursor = conn.execute("INSERT INTO scheduled_tasks(name,task_type,interval_days,next_run_date,params,enabled,created_at,updated_at) VALUES(?, 'agent_core',1,?,?,0,?,?)",
                (identity, task.deadline.date().isoformat(), encoded(payload), stamp, stamp))
            assert cursor.lastrowid is not None
            ident = int(cursor.lastrowid)
            self._record(conn, ident, "QUEUED", {"input_revision": task.input_revision})
            return ident

    def inspect(self, ident: int) -> dict[str, Any]:
        with self.journal.connect() as conn:
            row = conn.execute("SELECT r.status,r.message FROM scheduled_task_runs r JOIN scheduled_tasks t ON t.id=r.task_id WHERE r.task_id=? AND t.task_type='agent_core' ORDER BY r.id DESC LIMIT 1", (ident,)).fetchone()
            if row is None:
                raise ValueError("Unknown task")
            return {"task_id": ident, "state": row[0], "data": json.loads(row[1]), "production_database_modified": False}

    def transition(self, ident: int, state: State, data: dict[str, Any]) -> dict[str, Any]:
        with self.journal.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT r.status FROM scheduled_task_runs r JOIN scheduled_tasks t ON t.id=r.task_id WHERE r.task_id=? AND t.task_type='agent_core' ORDER BY r.id DESC LIMIT 1", (ident,)).fetchone()
            if previous is None:
                raise ValueError("Unknown task")
            if previous[0] == "CANCELLED":
                return self.inspect(ident)
            page = (data.get("result") or {}).get("findings", {}).get("classify_pending")
            if page and state in {"COMPLETED", "WAITING_FOR_DATA"}:
                key = "agent_queue:" + data["input_revision"]
                progress = json.loads(conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0])
                if progress["owner"] != ident or progress["offset"] != page["offset"]:
                    raise ValueError("Queue reservation changed")
                progress.update(owner=None, offset=page["processed_total"], total=page["total_pending"])
                conn.execute("UPDATE settings SET value=? WHERE key=?", (encoded(progress), key))
            self._record(conn, ident, state, data)
        return self.inspect(ident)

    def cancel(self, ident: int) -> dict[str, Any]:
        return self.transition(ident, "CANCELLED", {"reason": "explicit local cancellation", "financial_apply_allowed": False})

    def results(self, limit: int = 20) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ValueError("Bounded results required")
        revision = self.revision()
        with self.journal.connect() as conn:
            rows = conn.execute("SELECT id,params FROM scheduled_tasks WHERE task_type='agent_core' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        results = []
        for row in rows:
            task = AgentTask.model_validate_json(row[1])
            result = self.inspect(row[0])
            if task.intent == "financial_insights" and result["state"] == "COMPLETED" and task.input_revision == revision and not self._insights_current(task):
                result = {"task_id": row[0], "state": "WAITING_FOR_DATA", "data": {"reason": "accepted financial evidence no longer current"}}
            if task.research_policy_id and self.public_sources is not None and result["state"] != "CANCELLED":
                try:
                    ResearchPolicies(self.journal).check(task.research_policy_id, synthetic=self.public_sources.synthetic, now=self.clock())
                except PermissionError:
                    result = {"task_id": row[0], "state": "WAITING_FOR_APPROVAL", "data": {"reason": "research permission no longer active"}}
            if task.intent in {"financial_analysis", "owner_financial_analysis", "local_analysis"} and result["state"] == "COMPLETED" and not self._analysis_current(task, result["data"]):
                result = {"task_id": row[0], "state": "WAITING_FOR_DATA", "data": {"reason": "analysis evidence no longer current"}}
            if task.intent == "public_research" and result["state"] == "COMPLETED":
                sources = result["data"]["result"]["findings"]["read_public_sources"]["sources"]
                if self.public_sources is None or not self.public_sources.current(sources):
                    result = {"task_id": row[0], "state": "WAITING_FOR_DATA", "data": {"reason": "public source changed, revoked or stale"}}
            if result["state"] != "CANCELLED" and (task.input_revision != revision or self.clock() >= task.deadline):
                result = {"task_id": row[0], "state": "WAITING_FOR_DATA", "data": {"reason": "source changed or expired; result withheld"}}
            results.append(result)
        return {"status": "available", "tasks": results, "source_revision": revision, "production_database_modified": False}

    def _insights(self, task: AgentTask) -> dict[str, Any]:
        if task.month is None:
            return {"status": "evidence_only", "analysis_status": "insufficient_data", "cards": [],
                "notices": [quality_notice("comparison_period_missing")], "financial_apply_allowed": False}
        return financial_insights(self.assistant.ledger, month=task.month or "", currency=task.currency,
            now=self.clock(), accepted_sources=self.assistant.accepted_sources,
            accepted_sha256=self.assistant.accepted_sha256, policy=self.insight_policy)

    def _insights_current(self, task: AgentTask) -> bool:
        if self.assistant.ledger is None or task.month is None:
            return False
        return bool(financial_question(self.assistant.ledger, month=task.month, currency=task.currency,
            intent="category_growth", now=self.clock(), accepted_sources=self.assistant.accepted_sources,
            accepted_sha256=self.assistant.accepted_sha256)["status"] == "available")

    async def financial_insights_event(self, month: str, currency: str = "RUB", *, deadline: datetime | None = None) -> dict[str, Any]:
        event = "insights:" + hashlib.sha256(encoded([month, currency, self.insight_policy.model_dump(mode="json")]).encode()).hexdigest()
        task = AgentTask(intent="financial_insights", origin="event", event_key=event, input_revision=self.revision(),
            source_scope=("accepted_finance",), month=month, currency=currency, deadline=deadline or self.clock() + timedelta(days=1))
        return await self.run(self.submit(task))

    def insight_cards(self) -> dict[str, Any]:
        cards: dict[str, dict[str, Any]] = {}
        notices = []
        analyzed = False
        for task in self.results(50)["tasks"]:
            findings = task.get("data", {}).get("result", {}).get("findings", {})
            collection = findings.get("derive_financial_insights")
            if collection is None:
                if task["state"] == "WAITING_FOR_DATA":
                    notices.append(quality_notice("stale_or_invalidated_task"))
                continue
            notices.extend(collection.get("notices", []))
            analyzed |= collection["analysis_status"] == "available"
            for card in collection["cards"]:
                decision = self.journal.get_setting("insight_decision:" + card["id"])
                cards[card["id"]] = {**card, "task_id": task["task_id"], "binding": {**collection["binding"],
                    "analyzed_at": collection.get("observed_at"), "input_revision": collection.get("source_revision")},
                    "status": "current", "decision": decision}
        return {"status": "available" if analyzed else "insufficient_data", "analysis_status": "available" if analyzed else "insufficient_data", "cards": list(cards.values()),
            "notices": notices, "financial_apply_allowed": False, "production_database_modified": False}

    def decide_insight(self, task_id: int, card_id: str, action: str) -> dict[str, Any]:
        if action not in {"acknowledge", "dismiss", "reopen"}:
            raise ValueError("Unsupported insight action")
        card = next((c for c in self.insight_cards()["cards"] if c["id"] == card_id and c["task_id"] == task_id), None)
        if card is None:
            raise ValueError("Current source-linked insight required")
        decision = {"action": action, "card_id": card_id, "task_id": task_id}
        key = "insight_decision:" + card_id
        with self.journal.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if previous and json.loads(previous[0]) == decision:
                return {**decision, "cached": True, "production_database_modified": False}
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encoded(decision)))
            self.journal.audit(conn, "insight_human_decision", {**decision, "source_sha256": card["binding"]["source_sha256"]})
        return {**decision, "production_database_modified": False}

    def _public_evidence(self, task: AgentTask) -> dict[str, Any]:
        """Read an existing research result, never trigger or replay its transport."""
        if task.public_evidence_task is None or self.public_sources is None:
            raise PermissionError("Explicit public evidence task required")
        with self.journal.connect() as conn:
            row = conn.execute("SELECT params FROM scheduled_tasks WHERE id=? AND task_type='agent_core'", (task.public_evidence_task,)).fetchone()
        if row is None:
            raise PermissionError("Unknown public task")
        research = AgentTask.model_validate_json(row[0])
        result = self.inspect(task.public_evidence_task)
        if (research.intent != "public_research" or result["state"] != "COMPLETED"
                or research.input_revision != self.revision() or self.clock() >= research.deadline):
            raise PermissionError("Current public evidence required")
        if research.research_policy_id:
            ResearchPolicies(self.journal).check(research.research_policy_id, synthetic=self.public_sources.synthetic, now=self.clock())
        findings = result["data"]["result"]["findings"]
        sources = findings.get("read_public_sources", {}).get("sources", [])
        context = self.public_sources.explain(sources)
        if context["status"] != "evidence_only":
            raise PermissionError("Relevant public citations required")
        return {"sources": sources, "context": context}

    def _analysis_current(self, task: AgentTask, data: dict[str, Any]) -> bool:
        from jarvis.finance.financial_analyst import AnalystPermissions
        try:
            if task.intent == "local_analysis":
                prior = data["result"]["findings"]["local_statement_analysis"]["answer"]
                current = self.assistant.local_analysis(task.month or "", task.currency, self.clock())
                return bool(current.get("current") and current["source_revisions"] == prior["source_revisions"])
            finding = data["result"]["findings"]["analyze_accepted_aggregates"]
            evidence = AnalystPermissions(self.store).evidence(finding["permission_id"], task.month or "", synthetic=bool(finding["simulation"]))
            return bool(finding["source_binding"] == evidence["binding"] and
                    (task.intent == "owner_financial_analysis" or self._public_evidence(task)["sources"] == finding["public_sources"]))
        except (KeyError, ValueError, PermissionError, OSError, sqlite3.Error):
            return False

    async def financial_analysis_event(self, month: str, public_task: int | None = None, *, deadline: datetime,
                                       expected_permission_id: str | None = None, expected_binding: str | None = None) -> dict[str, Any]:
        """Analyze accepted aggregates under existing grants, optionally with public context."""
        from jarvis.finance.statement_product import StatementProduct
        # Materialize the immutable projection before capturing its input revision.
        StatementProduct(self.store).month_report(month, now=self.clock())
        task = AgentTask(intent="financial_analysis" if public_task is not None else "owner_financial_analysis", origin="financial_analysis", event_key="aggregate-ai:" + month,
            input_revision=self.revision(), source_scope=("accepted_finance", "public_research") if public_task is not None else ("accepted_finance",),
            public_evidence_task=public_task, month=month, expected_analyst_permission_id=expected_permission_id,
            expected_analyst_binding=expected_binding, maximum_steps=3, run_budget_rub="0.03", deadline=deadline,
            timeout_seconds=30)
        return await self.run(self.submit(task))

    async def local_analysis_event(self, month: str | None = None, currency: str = "RUB", *, deadline: datetime) -> dict[str, Any]:
        """Run once for the accepted source revision, without research or model calls."""
        from jarvis.finance.statement_product import StatementProduct
        if month is None:
            dates = [row["metadata"]["period"][1] for row in StatementProduct(self.store).sources()
                     if row["accepted"] and row["metadata"]["currency"] == currency]
            if not dates:
                return {"state": "WAITING_FOR_DATA", "answer": self.assistant.answer("insufficient_data", "LOCAL_AUTOPILOT",
                    "Нет принятой выписки. Проверка источника не заменяется нулевым балансом.", current=False),
                    "production_database_modified": False}
            month = max(dates)[:7]
        task = AgentTask(intent="local_analysis", origin="event", event_key="local-analysis:" + month + ":" + currency,
            input_revision=self.revision(), source_scope=("accepted_finance",), month=month, currency=currency,
            maximum_steps=2, deadline=deadline)
        return await self.run(self.submit(task))

    async def financial_research_event(self, month: str, policy_id: str, *, deadline: datetime) -> dict[str, Any]:
        """Choose a safe generic topic locally and reuse existing free-HTTP research."""
        from jarvis.finance.financial_analyst import public_research_need
        from jarvis.finance.statement_product import StatementProduct
        need = public_research_need(StatementProduct(self.store).month_report(month, now=self.clock()))
        if need['status'] != 'available':
            return {**need, 'production_database_modified': False}
        if self.public_sources is None or self.research_planner is not None:
            return {'status': 'blocked', 'reason': 'Нужен existing PublicSources без платного planner', 'decision': need}
        policy = ResearchPolicies(self.journal).check(policy_id, synthetic=self.public_sources.synthetic, now=self.clock())
        if (not policy.free_http or policy.paid_llm or need['topic'] not in policy.topics
                or self.public_sources.question != need['topic']):
            raise PermissionError('Selected safe topic/free HTTP scope not approved')
        task = AgentTask(intent='public_research', origin='financial_analysis', month=month,
            event_key='web-discovery:finance:' + policy_id + ':' + month,
            input_revision=self.revision(), source_scope=('accepted_finance', 'public_research'),
            research_policy_id=policy_id, maximum_steps=4, deadline=min(deadline, policy.expires_at))
        return await self.run(self.submit(task))

    async def run_once(self) -> dict[str, Any]:
        """Dispatch only queued agent rows by priority; no job registration."""
        with self.journal.connect() as conn:
            rows = conn.execute("SELECT t.id,t.params FROM scheduled_tasks t JOIN scheduled_task_runs r ON r.id=(SELECT MAX(id) FROM scheduled_task_runs WHERE task_id=t.id) WHERE t.task_type='agent_core' AND r.status='QUEUED'").fetchall()
        if not rows:
            return {"state": "WAITING_FOR_DATA", "reason": "no queued agent task"}
        ident = min(rows, key=lambda row: (AgentTask.model_validate_json(row[1]).priority, row[0]))[0]
        return await self.run(ident)

    def recover_interrupted(self, ident: int) -> dict[str, Any]:
        """Explicit recovery marks running work UNKNOWN, never automatically replays it."""
        if self.inspect(ident)["state"] == "RUNNING":
            return self.transition(ident, "UNKNOWN", {"reason": "interrupted worker; no automatic replay"})
        return self.inspect(ident)

    async def on_source_refresh(self, event_key: str, *, deadline: datetime) -> dict[str, Any]:
        if self.assistant.ledger is not None:
            period = (self.clock().date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            return await self.financial_insights_event(period, deadline=deadline)
        from jarvis.finance.statement_product import StatementProduct
        if any(source['accepted'] for source in StatementProduct(self.store).sources()):
            return await self.local_analysis_event(deadline=deadline)
        task = AgentTask(intent="review_overview", origin="data_refresh", event_key=event_key,
            input_revision=self.revision(), source_scope=("review",), deadline=deadline)
        return await self.run(self.submit(task))

    async def workflow_event(self, event_key: str, *, deadline: datetime, month: str | None = None) -> dict[str, Any]:
        revision = self.revision()
        progress = self.journal.get_setting("agent_queue:" + revision, {"offset": 0, "owner": None})
        total = sum(row["status"] == "pending_review" for row in self.store.items())
        if progress["offset"] >= total and progress.get("total") == total:
            return {"state": "WAITING_FOR_DATA", "cached": True, "queue_exhausted": True,
                    "processed_total": total, "remaining": 0, "source_revision": revision}
        if progress["owner"] is not None and self.inspect(progress["owner"])["state"] not in {"FAILED", "CANCELLED", "WAITING_FOR_APPROVAL"}:
            return {"state": "WAITING_FOR_DATA", "reason": "active or unknown batch reservation", "task_id": progress["owner"]}
        task = AgentTask(intent="workflow", origin="data_refresh", event_key=event_key, input_revision=revision,
                         source_scope=("review", "accepted_finance"), batch_offset=progress["offset"], deadline=deadline,
                         maximum_steps=4, month=month)
        return await self.run(self.submit(task))

    async def run(self, ident: int) -> dict[str, Any]:
        if self.clock().tzinfo is None:
            raise ValueError("Aware runtime clock required")
        with self.journal.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT params FROM scheduled_tasks WHERE id=? AND task_type='agent_core'", (ident,)).fetchone()
            if row is None:
                raise ValueError("Unknown agent task")
            task = AgentTask.model_validate_json(row[0])
            previous = conn.execute("SELECT status,message FROM scheduled_task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (ident,)).fetchone()
            if task.intent == "public_research" and self.public_sources is not None and previous[0] != "CANCELLED":
                try:
                    if task.research_policy_id:
                        ResearchPolicies(self.journal).check(task.research_policy_id, synthetic=self.public_sources.synthetic, now=self.clock())
                    elif not self.public_sources.synthetic:
                        raise PermissionError("Live public research requires owner policy")
                except PermissionError:
                    self._record(conn, ident, "WAITING_FOR_APPROVAL", {"reason": "active research permission required"})
                    return {"task_id": ident, "state": "WAITING_FOR_APPROVAL", "data": {"reason": "active research permission required"}}
            if previous[0] != "QUEUED":
                old = {"task_id": ident, "state": previous[0], "data": json.loads(previous[1]), "cached": True}
                if task.intent == "financial_insights" and previous[0] == "COMPLETED" and task.input_revision == self.revision() and not self._insights_current(task):
                    self._record(conn, ident, "WAITING_FOR_DATA", {"reason": "accepted financial source stale or revoked"})
                    return {**old, "state": "WAITING_FOR_DATA", "data": {"reason": "accepted financial source stale or revoked"}}
                if task.intent in {"financial_analysis", "owner_financial_analysis", "local_analysis"} and previous[0] == "COMPLETED" and not self._analysis_current(task, old["data"]):
                    self._record(conn, ident, "WAITING_FOR_DATA", {"reason": "analysis evidence no longer current"})
                    return {**old, "state": "WAITING_FOR_DATA", "data": {"reason": "analysis evidence no longer current"}}
                if task.intent == "public_research" and previous[0] == "COMPLETED":
                    sources = old["data"]["result"]["findings"]["read_public_sources"]["sources"]
                    if self.public_sources is None or not self.public_sources.current(sources):
                        self._record(conn, ident, "WAITING_FOR_DATA", {"reason": "public source changed, revoked or stale"})
                        return {**old, "state": "WAITING_FOR_DATA", "data": {"reason": "public evidence no longer current"}}
                if previous[0] != "CANCELLED" and (task.input_revision != self.revision() or (previous[0] == "COMPLETED" and self.clock() >= task.deadline)):
                    reason = "source revision or result expiry changed; cached result withheld"
                    self._record(conn, ident, "WAITING_FOR_DATA", {"reason": reason})
                    return {**old, "state": "WAITING_FOR_DATA", "data": {"reason": reason}}
                return old
            if task.intent in {"classification_preview", "workflow"}:
                key = "agent_queue:" + task.input_revision
                row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                progress = json.loads(row[0]) if row else {"offset": 0, "owner": None}
                if progress["owner"] is not None:
                    owner = conn.execute("SELECT status FROM scheduled_task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (progress["owner"],)).fetchone()
                    if owner is None or owner[0] not in {"FAILED", "CANCELLED", "WAITING_FOR_APPROVAL"}:
                        self._record(conn, ident, "WAITING_FOR_DATA", {"reason": "batch reserved; no automatic replay"})
                        return {"state": "WAITING_FOR_DATA", "task_id": ident, "reason": "batch reserved; no automatic replay"}
                if task.batch_offset != progress["offset"]:
                    self._record(conn, ident, "WAITING_FOR_DATA", {"reason": "stale batch offset"})
                    return {"state": "WAITING_FOR_DATA", "task_id": ident, "reason": "stale batch offset"}
                progress["owner"] = ident
                conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encoded(progress)))
            self._record(conn, ident, "RUNNING", {"input_revision": task.input_revision})
        observations: dict[str, Any] = {}
        current_tool: str | None = None
        registry = ToolRegistry()
        def permission(kind: Literal["task", "step", "http", "discovery"], effect: str) -> None:
            if task.research_policy_id and self.public_sources:
                ResearchPolicies(self.journal).consume(task.research_policy_id, ident, kind, effect,
                    synthetic=self.public_sources.synthetic, question=self.public_sources.question, now=self.clock())
        async def candidates(_: ToolInput) -> dict[str, Any]:
            if self.public_sources and self.public_sources.search_endpoint:
                if not task.research_policy_id:
                    return {"status": "blocked", "reason": "Discovery requires purpose-bound owner policy"}
                decision = None
                if task.origin == "financial_analysis":
                    from jarvis.finance.financial_analyst import public_research_need
                    from jarvis.finance.statement_product import StatementProduct
                    decision = public_research_need(StatementProduct(self.store).month_report(task.month or "", now=self.clock()))
                    if decision['status'] != 'available' or self.public_sources.question != decision['topic'] or self.research_planner is not None:
                        raise PermissionError('Financial public topic changed/refused')
                discovered = await self.public_sources.discover(lambda: self.inspect(ident)["state"] != "CANCELLED"
                    and self.clock() < task.deadline and self.revision() == task.input_revision,
                    lambda url: permission("discovery", url))
                if decision is not None:
                    discovered['financial_research_decision'] = decision
                return discovered
            return self.public_sources.candidates() if self.public_sources else {"status": "blocked", "reason": "Public sources not configured"}

        async def read_public(_: ToolInput) -> dict[str, Any]:
            if self.public_sources is None:
                return {"status": "blocked"}
            return await self.public_sources.read(lambda: self.inspect(ident)["state"] != "CANCELLED"
                and self.clock() < task.deadline and self.revision() == task.input_revision,
                lambda url: permission("http", url), discovery=observations.get("public_candidates"))

        async def explain_public(_: ToolInput) -> dict[str, Any]:
            if self.public_sources is None:
                return {"status": "blocked"}
            return self.public_sources.explain(observations["read_public_sources"]["sources"])

        registry.register("public_candidates", "AUTO_READ", candidates)
        registry.register("read_public_sources", "LOCAL_DERIVED_WRITE", read_public)
        registry.register("explain_public_sources", "AUTO_READ", explain_public)
        async def pending(_: ToolInput) -> dict[str, Any]:
            items = self.store.items()
            return {"status": "available", "pending_count": sum(i["status"] == "pending_review" for i in items),
                    "review_ids": [i["id"] for i in items if i["status"] == "pending_review"][:50], "financial_apply_allowed": False}
        async def labels(_: ToolInput) -> dict[str, Any]:
            memory = HumanLabels(self.store).memory(now=self.clock())
            return {"status": "available", "human_labels": len(memory["facts"]), "memory_revision": memory["snapshot_sha256"], "training_allowed": False}
        async def finance(_: ToolInput) -> dict[str, Any]:
            if task.month is None:
                return {"status": "insufficient_data", "reason": "explicit month required"}
            return await self.assistant.route("Сколько я потратил?", task.month, task.currency, False, self.clock())
        async def goal(_: ToolInput) -> dict[str, Any]:
            if task.scenario is None or task.scenario.mode != "hypothetical_goal":
                return {"status": "insufficient_data", "reason": "explicit hypothetical goal inputs required"}
            return calculate_scenario(self.store, task.scenario, now=self.clock())
        async def classify(_: ToolInput) -> dict[str, Any]:
            from jarvis.finance.classification.suggestions import LocalSuggestions
            suggestions = LocalSuggestions(self.store)
            items = [row for row in self.store.items() if row["status"] == "pending_review"]
            selected = items[task.batch_offset:task.batch_offset + 20]
            proposals = [proposal.model_dump() for proposal in suggestions.preview_items([row["id"] for row in selected])] if selected else []
            groups = []
            for reason in {proposal["reason_code"] for proposal in proposals}:
                ids = [proposal["item_id"] for proposal in proposals if proposal["reason_code"] == reason]
                groups.append({"reason": reason, "item_ids": ids, "count": len(ids), "navigation_only": True, "apply_allowed": False})
            merchants: dict[str, list[str]] = {}
            by_id = {row["id"]: row for row in selected}
            for proposal in proposals:
                if proposal["suggested_category"] is None:
                    merchant = " ".join(str(by_id[proposal["item_id"]]["operation"].get("merchant", "")).casefold().split())
                    if merchant:
                        merchants.setdefault(hashlib.sha256(merchant.encode()).hexdigest(), []).append(proposal["item_id"])
            for merchant_hash, ids in merchants.items():
                if len(ids) > 1:
                    groups.append({"reason": "repeated_uncertain_merchant", "merchant_hash": merchant_hash,
                                   "item_ids": ids, "count": len(ids), "navigation_only": True, "apply_allowed": False})
            return {"status": "available", "suggestions": proposals, "groups": sorted(groups, key=lambda g: g["reason"]),
                    "offset": task.batch_offset, "processed_total": task.batch_offset + len(selected),
                    "rule_model_comparison": "unverified_no_verified_model",
                    "total_pending": len(items), "not_processed": max(0, len(items) - task.batch_offset - len(selected)),
                    "cloud_status": "blocked", "human_confirmation_required": True, "financial_apply_allowed": False}
        async def diagnostics(_: ToolInput) -> dict[str, Any]:
            assessment = await finance(ToolInput()) if "accepted_finance" in task.source_scope else {"status": "blocked", "reason": "financial source capability required"}
            return {"status": "available", "analysis_status": assessment.get("status", "unavailable"), "assessment": assessment,
                    "signals": ["pending_review", "source_coverage_requires_confirmation"], "causal_claims": False}
        async def insights(_: ToolInput) -> dict[str, Any]:
            return self._insights(task)
        async def local_analysis(_: ToolInput) -> dict[str, Any]:
            response = self.assistant.local_analysis(task.month or "", task.currency, self.clock())
            return {"status": "evidence_only" if response.get("current") else "insufficient_data", "answer": response}

        registry.register("local_statement_analysis", "AUTO_READ", local_analysis)
        async def accepted_aggregates(_: ToolInput) -> dict[str, Any]:
            from jarvis.finance.financial_analyst import (
                AnalystPermissions,
                selected_analyst,
            )
            from jarvis.finance.statement_product import StatementProduct
            if task.intent == "financial_analysis" and (not isinstance(self.assistant.transport, httpx.MockTransport) or self.public_sources is None
                    or not self.public_sources.synthetic or task.run_budget_rub != "0.03"
                    or "public_research" not in task.source_scope or task.research_policy_id is not None):
                raise PermissionError("Offline analysis only; no live delegation")
            if task.month is None or task.currency != "RUB":
                raise PermissionError("Explicit RUB month required")
            selected = selected_analyst(self.store)
            if task.expected_analyst_permission_id is not None and task.expected_analyst_permission_id != selected['permission_id']:
                raise PermissionError('Owner preview permission changed before execution')
            permissions = AnalystPermissions(self.store)
            policy = permissions.check(selected["permission_id"], synthetic=isinstance(self.assistant.transport, httpx.MockTransport))
            if policy.currency != task.currency or policy.request_budget_rub > Decimal(task.run_budget_rub):
                raise PermissionError("Analyst currency/budget scope refused")
            evidence = permissions.evidence(selected["permission_id"], task.month, synthetic=isinstance(self.assistant.transport, httpx.MockTransport))
            if task.expected_analyst_binding is not None and task.expected_analyst_binding != evidence['binding']:
                raise PermissionError('Owner preview source changed before execution')
            public = self._public_evidence(task) if task.intent == "financial_analysis" else {"sources": [], "context": {"citations": []}}
            report = StatementProduct(self.store).month_report(task.month, task.currency, now=self.clock())
            return {"status": "available", "snapshot_version": report["version"], "source_sha256": report["source_sha256"],
                    "period": report["period"], "currency": report["currency"], "public": public,
                    "source_binding": evidence["binding"], "permission_id": selected["permission_id"]}

        async def analyze_aggregates(_: ToolInput) -> dict[str, Any]:
            from jarvis.finance.financial_analyst import analyze_month
            source = observations["accepted_aggregates"]
            if task.intent == "financial_analysis" and self._public_evidence(task) != source["public"]:
                raise PermissionError("Public evidence changed before analysis")
            from jarvis.core.llm.errors import LLMBudgetExceeded
            try:
                result = await analyze_month(self.store, task.month or "", transport=self.assistant.transport, expected_binding=source["source_binding"], expected_permission_id=source["permission_id"])
            except LLMBudgetExceeded:
                return {"status": "blocked", "reason": "analyst preflight budget refused; no completion sent"}
            if result["status"] != "analysis":
                return result  # UNKNOWN/invalid answer is never a successful task.
            if result["snapshot_version"] != source["snapshot_version"]:
                raise PermissionError("Financial snapshot changed during analysis")
            return {**result, "status": "available", "permission_id": source["permission_id"],
                    "public_sources": source["public"]["sources"], "public_context": source["public"]["context"],
                    "answer": self.assistant.answer("evidence_only", "FINANCIAL_ANALYST", result["text"],
                        data={"snapshot_version": result["snapshot_version"], "period": source["period"], "currency": source["currency"], "reasoning": result.get("reasoning")},
                        citations=source["public"]["context"]["citations"], simulation=result["simulation"],
                        model=result["model"], source_revisions=[result["source_binding"], result["source_sha256"]],
                        limitations=["Public citations are separate context; Qwen sees only aggregates, not pages or merchants.",
                                     "Economic completeness and current balances are not inferred."])}

        registry.register("accepted_aggregates", "AUTO_READ", accepted_aggregates)
        registry.register("analyze_accepted_aggregates", "LOCAL_DERIVED_WRITE", analyze_aggregates)
        async def report(_: ToolInput) -> dict[str, Any]:
            return {"status": "available", "findings": dict(observations), "input_revision": task.input_revision,
                    "actual_ai_cost_rub": observations.get("analyze_accepted_aggregates", observations.get("research_planner", {})).get("paid_cost_rub", "0"),
                    "paid_calls": int(observations.get("analyze_accepted_aggregates", observations.get("research_planner", {})).get("simulation") is False), "financial_apply_allowed": False}
        for name, handler in (("pending_reviews", pending), ("label_readiness", labels), ("calculate_expenses", finance), ("calculate_goal_scenario", goal), ("classify_pending", classify), ("source_diagnostics", diagnostics)):
            registry.register(name, "AUTO_READ", handler)
        registry.register("prepare_report", "LOCAL_DERIVED_WRITE", report)
        registry.register("derive_financial_insights", "AUTO_READ", insights)
        plans = {"review_overview": ("pending_reviews", "label_readiness", "prepare_report"),
                 "financial_report": ("calculate_expenses", "prepare_report"), "goal_scenario": ("calculate_goal_scenario", "prepare_report"),
                 "local_analysis": ("local_statement_analysis", "prepare_report"),
                 "owner_financial_analysis": ("accepted_aggregates", "analyze_accepted_aggregates", "prepare_report"),
                 "financial_analysis": ("accepted_aggregates", "analyze_accepted_aggregates", "prepare_report"),
                 "financial_insights": ("derive_financial_insights", "prepare_report"),
                 "classification_preview": ("classify_pending", "label_readiness", "prepare_report"),
                 "workflow": ("classify_pending", "label_readiness", "source_diagnostics", "prepare_report"),
                 "public_research": ("public_candidates", "read_public_sources", "explain_public_sources", "prepare_report")}
        required_scope = {"review_overview": "review", "financial_report": "accepted_finance", "financial_analysis": "accepted_finance", "owner_financial_analysis": "accepted_finance", "local_analysis": "accepted_finance", "financial_insights": "accepted_finance", "goal_scenario": "hypothetical", "classification_preview": "review", "workflow": "review", "public_research": "public_research"}[task.intent]
        plan = plans[task.intent]
        try:
            permission("task", "claim")
            if required_scope not in task.source_scope or any(registry.permission(n) not in task.allowed_capabilities for n in plan):
                return self.transition(ident, "WAITING_FOR_APPROVAL", {"reason": "source/capability scope missing"})
            required_steps = len(plan) + int(task.intent == "public_research" and self.research_planner is not None)
            if required_steps > task.maximum_steps:
                return self.transition(ident, "FAILED", {"reason": "execution step limit"})
            if task.research_policy_id and self.public_sources:
                authorized = ResearchPolicies(self.journal).check(task.research_policy_id, synthetic=self.public_sources.synthetic, now=self.clock())
                if required_steps > authorized.maximum_steps or any(name not in authorized.tools for name in plan):
                    raise PermissionError("Policy plan/tool limits refused")
            if task.intent == "public_research" and self.research_planner is not None:
                from jarvis.core.llm.research_plan import ResearchPlan
                if self.public_sources is None or (not self.public_sources.synthetic and not task.research_policy_id):
                    return self.transition(ident, "WAITING_FOR_APPROVAL", {"reason": "Planner live policy not configured"})
                if self.clock() >= task.deadline or task.input_revision != self.revision():
                    return self.transition(ident, "WAITING_FOR_DATA", {"reason": "Planner input expired"})
                permission("step", "planner")
                if task.research_policy_id and not self.public_sources.synthetic and task.run_budget_rub != "0.03":
                    raise PermissionError("Paid task ceiling required")
                choice = await asyncio.wait_for(self.research_planner(str(ident) + task.input_revision), task.timeout_seconds)
                if choice.get("status") != "available":
                    return self.transition(ident, "WAITING_FOR_DATA", {"reason": "Planner unavailable", "observation": choice})
                approved = ResearchPlan.model_validate(choice["plan"])
                plan = tuple(approved.tools)
                observations["research_planner"] = choice
            for name in plan:
                if self.inspect(ident)["state"] == "CANCELLED":
                    return self.inspect(ident)
                if self.clock() >= task.deadline:
                    return self.transition(ident, "FAILED", {"reason": "task deadline"})
                if self.revision() != task.input_revision:
                    return self.transition(ident, "WAITING_FOR_DATA", {"reason": "source changed", "result": None})
                permission("step", name)
                current_tool = name
                result = await asyncio.wait_for(registry.call(name, {}, task.allowed_capabilities), task.timeout_seconds)
                result.update(source_revision=task.input_revision, observed_at=self.clock().isoformat(),
                              source_scope=list(task.source_scope), freshness_basis="task deadline plus tool-specific evidence; no live source probe")
                if result.get("status") not in {"available", "hypothetical", "experimental", "evidence_only"}:
                    from jarvis.core.llm.planner_diagnostics import sanitized
                    return self.transition(ident, "WAITING_FOR_DATA", {"reason": "tool data unavailable", "tool": name, "observation": result,
                        "model_completion": sanitized(observations.get("research_planner", {}).get("diagnostics"))})
                observations[name] = result
                self.transition(ident, "RUNNING", {"completed_tools": list(observations), "input_revision": task.input_revision})
            if self.revision() != task.input_revision:
                return self.transition(ident, "WAITING_FOR_DATA", {"reason": "source changed before result", "result": None})
            if self.clock() >= task.deadline:
                return self.transition(ident, "FAILED", {"reason": "deadline passed before result", "result": None})
            if task.research_policy_id and self.public_sources:
                ResearchPolicies(self.journal).check(task.research_policy_id, synthetic=self.public_sources.synthetic, now=self.clock())
            if task.intent == "public_research" and (self.public_sources is None or not self.public_sources.current(observations["read_public_sources"]["sources"])):
                return self.transition(ident, "WAITING_FOR_DATA", {"reason": "public sources changed before result", "result": None})
            if task.intent in {"financial_analysis", "owner_financial_analysis", "local_analysis"} and not self._analysis_current(task, {"result": observations["prepare_report"]}):
                return self.transition(ident, "WAITING_FOR_DATA", {"reason": "analysis evidence changed before publication"})
            state: State = "WAITING_FOR_DATA" if any(observations.get(k, {}).get("analysis_status", "available") != "available" for k in ("source_diagnostics", "derive_financial_insights")) else "COMPLETED"
            return self.transition(ident, state, {"result": observations["prepare_report"],
                "result_revision": hashlib.sha256(encoded(observations["prepare_report"]).encode()).hexdigest(), "input_revision": task.input_revision})
        except asyncio.CancelledError:
            self.transition(ident, "UNKNOWN", {"reason": "worker cancelled; no automatic replay"})
            raise
        except TimeoutError:
            from jarvis.core.llm.planner_diagnostics import sanitized
            return self.transition(ident, "UNKNOWN", {"reason": "tool timeout; no automatic replay",
                "model_completion": sanitized(observations.get("research_planner", {}).get("diagnostics")), "failed_tool": current_tool})
        except LLMError as exc:
            from jarvis.core.llm.planner_diagnostics import sanitized
            return self.transition(ident, "UNKNOWN", {"reason": "planner outcome unavailable; budget journal retained; no automatic replay",
                "diagnostics": sanitized(getattr(exc, "diagnostics", None))})
        except (BudgetReservationDenied, BudgetStoreUnavailable) as exc:
            from jarvis.core.llm.planner_diagnostics import sanitized
            return self.transition(ident, "WAITING_FOR_APPROVAL", {"reason": "planner budget refused/unavailable",
                "diagnostics": sanitized(getattr(exc, "planner_diagnostics", None))})
        except (ValueError, OSError, PermissionError, sqlite3.Error, httpx.HTTPError, KnowledgeUnavailable,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            from jarvis.core.llm.planner_diagnostics import sanitized
            return self.transition(ident, "FAILED", {"reason": "typed tool/source refusal",
                "diagnostics": sanitized(getattr(exc, "planner_diagnostics", None)),
                "model_completion": sanitized(observations.get("research_planner", {}).get("diagnostics")),
                "failed_tool": current_tool})
