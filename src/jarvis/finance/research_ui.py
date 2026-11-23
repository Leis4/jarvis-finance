"""Owner-scoped public discovery routes over the existing agent and lifecycle journal."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field

from jarvis.finance.scheduler.agent import AgentRuntime, AgentTask
from jarvis.finance.scheduler.research_policy import ResearchPolicies, ResearchPolicy
from jarvis.knowledge.journal import IndexJournal
from jarvis.knowledge.public_sources import PublicSources


class PublicSearchGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: ResearchPolicy
    public_only_confirmed: bool = Field(strict=True)


class FinancialSearchRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    month: str = Field(pattern=r"^\d{4}-\d{2}$")


class PublicSearchRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")


def install_research_routes(app: FastAPI, authorized: Callable[..., Any], core: AgentRuntime | None,
                            transport: httpx.MockTransport | None = None) -> Callable[[], dict[str, Any]]:
    """Keep grants, tools, reports and replay in existing guarded staging stores."""
    def required() -> AgentRuntime:
        if core is None:
            raise ValueError("Configure the existing private agent journal first")
        return core

    def runtime(ident: str) -> AgentRuntime:
        base = required()
        policy = ResearchPolicies(base.journal).check(ident, synthetic=transport is not None)
        if not policy.search_endpoint or len(policy.topics) != 1:
            raise PermissionError("One public topic and discovery endpoint required")
        folder = base.journal.path.parent / "public-research"
        if folder.is_symlink() or any(p.is_symlink() for p in folder.parents):
            raise PermissionError("Linked public evidence path refused")
        folder.mkdir(exist_ok=True)
        path = folder / "lineage.sqlite"
        if path.is_symlink() or path.with_suffix(".sqlite.lock").is_symlink():
            raise PermissionError("Linked public journal refused")
        sources = PublicSources(IndexJournal(path), policy.urls, frozenset(policy.domains), transport=transport,
            question=policy.topics[0], search_endpoint=policy.search_endpoint, discovery_scope=ident)
        return AgentRuntime(base.journal, base.store, assistant=base.assistant, public_sources=sources,
                            insight_policy=base.insight_policy, clock=base.clock)

    @app.get("/api/research/status", dependencies=[Depends(authorized)])
    def status() -> dict[str, Any]:
        return {"status": "AWAITING_OWNER", "journal_configured": core is not None,
            "mode": "mock" if transport is not None else "live", "provider": "searxng",
            "paid_search": False, "paid_llm": False, "network_verified": False,
            "reason": "Configure a JSON-enabled HTTPS SearXNG instance and approve one public topic and source domains"}

    @app.post("/api/research/grant", dependencies=[Depends(authorized)])
    def grant(body: PublicSearchGrant) -> dict[str, Any]:
        if (not body.public_only_confirmed or body.policy.mode != ("mock" if transport is not None else "live")
                or body.policy.paid_llm or not body.policy.search_endpoint or len(body.policy.topics) != 1):
            raise PermissionError("Separate public-only free HTTP permission required")
        ident = ResearchPolicies(required().journal).grant(body.policy, owner_action=True)
        return {"status": "granted", "policy_id": ident, "network_requests": 0, "production_database_modified": False}

    @app.post("/api/research/{policy_id}/revoke", dependencies=[Depends(authorized)])
    def revoke(policy_id: str) -> dict[str, Any]:
        ResearchPolicies(required().journal).revoke(policy_id)
        return {"status": "revoked", "production_database_modified": False}

    @app.get("/api/research/{policy_id}/scope", dependencies=[Depends(authorized)])
    def scope(policy_id: str) -> dict[str, Any]:
        policy = ResearchPolicies(required().journal).check(policy_id, synthetic=transport is not None)
        return {"topic": policy.topics[0], "domains": list(policy.domains), "search_endpoint": policy.search_endpoint,
                "expires_at": policy.expires_at.isoformat(), "mode": policy.mode, "paid_search": False, "paid_llm": False}

    @app.post("/api/research/run", dependencies=[Depends(authorized)])
    async def run(body: PublicSearchRun) -> dict[str, Any]:
        active = runtime(body.policy_id)
        policy = ResearchPolicies(active.journal).check(body.policy_id, synthetic=transport is not None)
        task = AgentTask(intent="public_research", origin="user", event_key="web-discovery:" + body.policy_id + ":" + body.request_id,
            research_policy_id=body.policy_id, input_revision=active.revision(), source_scope=("public_research",),
            maximum_steps=4, timeout_seconds=30, deadline=policy.expires_at)
        return await active.run(active.submit(task))

    @app.get("/api/research/financial-need", dependencies=[Depends(authorized)])
    def financial_need(month: str) -> dict[str, Any]:
        from jarvis.finance.financial_analyst import public_research_need
        from jarvis.finance.statement_product import StatementProduct
        try:
            need = public_research_need(StatementProduct(required().store).month_report(month, now=required().clock()))
        except ValueError:
            return {'status': 'insufficient_data', 'topic': None, 'network_requests': 0}
        return {**need, 'network_requests': 0, 'permission_created': False}

    @app.post("/api/research/financial", dependencies=[Depends(authorized)])
    async def financial(body: FinancialSearchRun) -> dict[str, Any]:
        active = runtime(body.policy_id)
        policy = ResearchPolicies(active.journal).check(body.policy_id, synthetic=transport is not None)
        return await active.financial_research_event(body.month, body.policy_id, deadline=policy.expires_at)

    @app.get("/api/research/tasks", dependencies=[Depends(authorized)])
    def results() -> dict[str, Any]:
        if core is None:
            return {"status": "AWAITING_OWNER", "tasks": []}
        with core.journal.connect() as conn:
            rows = conn.execute("SELECT id,params FROM scheduled_tasks WHERE task_type='agent_core' AND json_extract(params,'$.event_key') LIKE 'web-discovery:%' ORDER BY id DESC LIMIT 20").fetchall()
        tasks = []
        for ident, params in rows:
            try:
                bound = runtime(json.loads(params)["research_policy_id"])
                result = bound.inspect(ident)
                if result["state"] == "COMPLETED":
                    sources = result["data"]["result"]["findings"]["read_public_sources"]["sources"]
                    if result["data"].get("input_revision") != bound.revision() or bound.public_sources is None or not bound.public_sources.current(sources):
                        result = {"task_id": ident, "state": "WAITING_FOR_DATA", "reason": "Source changed, revoked or stale; prior report is historical"}
                tasks.append(result)
            except (PermissionError, ValueError):
                tasks.append({"task_id": ident, "state": "WAITING_FOR_DATA", "reason": "Public permission revoked/expired or evidence unavailable"})
        return {"status": "available", "tasks": tasks, "observed_at": datetime.now(UTC).isoformat(), "production_database_modified": False}

    return results
