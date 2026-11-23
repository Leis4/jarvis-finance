"""Narrow owner-only chat capability; never grants bank, ledger or shell writes."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from jarvis.services.cognitive import ID, CognitiveConversation, Consent, screened


class ConsentInput(Consent):
    confirmed: Literal[True] = Field(...)


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=ID)
    request_id: str = Field(pattern=ID)
    message: str = Field(min_length=1, max_length=2000)


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["preference", "hypothesis", "prediction"]
    text: str = Field(min_length=1, max_length=500)
    confirmed: Literal[True]


class InsightFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["confirm", "correct", "reject", "not_relevant", "remind_later"]
    note: str = Field(default="", max_length=500)
    remind_minutes: int = Field(default=1440, ge=1, le=10080)


class MaintenanceProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    component: Literal["provider", "budget", "web_ui", "sources"]
    hypothesis: str = Field(max_length=1000)
    suggested_test: str = Field(max_length=500)
    confirmed: Literal[True]


def cognitive_route(method: str, path: str) -> bool:
    """Closed journal-only exceptions; handlers still enforce consent/capabilities."""
    exact = {
        "GET": {
            "/api/cognitive/status",
            "/api/cognitive/history",
            "/api/cognitive/diagnostics",
            "/api/cognitive/intelligence",
        },
        "PUT": {"/api/cognitive/consent"},
        "POST": {
            "/api/cognitive/turn",
            "/api/cognitive/memory",
            "/api/cognitive/maintenance-proposals",
        },
    }
    if path in exact.get(method, set()):
        return True
    if method == "GET" and re.fullmatch(r"/api/cognitive/tasks/[0-9a-f]{32}", path):
        return True
    if method == "POST" and re.fullmatch(
        r"/api/cognitive/tasks/[0-9a-f]{32}/cancel", path
    ):
        return True
    if method == "POST" and re.fullmatch(r"/api/cognitive/insights/[a-f0-9]{8,64}/feedback", path):
        return True
    return (
        method == "DELETE"
        and re.fullmatch(r"/api/cognitive/memory/[0-9a-f]{64}", path) is not None
    )


def install_cognitive_routes(
    app: FastAPI, service: CognitiveConversation, authorized: Any
) -> None:
    app.state.cognitive = service

    @app.exception_handler(RequestValidationError)
    async def safe_validation(request: Request, exc: RequestValidationError) -> Any:
        if request.url.path.startswith("/api/cognitive/"):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "Неверный ограниченный запрос; секреты не отражаются в ошибках."
                },
            )
        return await request_validation_exception_handler(request, exc)

    @app.get("/api/cognitive/status", dependencies=[Depends(authorized)])
    def status() -> dict[str, Any]:
        return service.status()

    @app.put("/api/cognitive/consent", dependencies=[Depends(authorized)])
    async def consent(body: ConsentInput) -> dict[str, Any]:
        return service.set_consent(body)

    @app.get("/api/cognitive/history", dependencies=[Depends(authorized)])
    def history(conversation_id: str = Query(pattern=ID)) -> dict[str, Any]:
        return {
            "turns": service.history(conversation_id),
            "retention_days": 7,
            "historical": True,
        }

    @app.post("/api/cognitive/turn", dependencies=[Depends(authorized)])
    async def turn(body: TurnInput) -> dict[str, Any]:
        try:
            return {
                "task_id": await service.start(
                    body.conversation_id, body.request_id, body.message
                )
            }
        except PermissionError:
            raise HTTPException(
                403, "Явное согласие на облачный чат не включено"
            ) from None
        except ValueError:
            raise HTTPException(
                409,
                "Запрос отклонён: повтор ID с другим содержимым,секрет или активная генерация",
            ) from None

    @app.get("/api/cognitive/tasks/{ident}", dependencies=[Depends(authorized)])
    def task(ident: str) -> dict[str, Any]:
        return service.task(ident)

    @app.post("/api/cognitive/tasks/{ident}/cancel", dependencies=[Depends(authorized)])
    async def cancel(ident: str) -> dict[str, Any]:
        return await service.cancel(ident)

    @app.post("/api/cognitive/memory", dependencies=[Depends(authorized)])
    def memory(body: MemoryInput) -> dict[str, Any]:
        text = screened(body.text)
        ident = service.store.record_memory_note(
            body.kind,
            text,
            actor="owner",
            model_version="owner-note-v1" if body.kind == "prediction" else None,
            expires_at=datetime.now(UTC) + timedelta(days=30)
            if body.kind == "prediction"
            else None,
        )
        return {
            "id": ident,
            "kind": body.kind,
            "text": text,
            "status": "confirmed_nonfinancial_note",
            "fact_authority": False,
        }

    @app.delete("/api/cognitive/memory/{ident}", dependencies=[Depends(authorized)])
    def revoke(ident: str) -> dict[str, Any]:
        note = next((n for n in service.memory() if n["id"] == ident), None)
        if note is None:
            raise HTTPException(404, "Активная заметка не найдена")
        service.store.revoke_memory_note(
            ident, actor="owner", expected_revision=note["revision"]
        )
        return {"status": "revoked"}

    @app.get("/api/cognitive/intelligence", dependencies=[Depends(authorized)])
    def intelligence() -> dict[str, Any]:
        from jarvis.finance.monthly_analysis import (
            chart_from_report,
            forecast,
            monthly_report,
        )
        from jarvis.services.budget_policy import telemetry as budget_telemetry
        from jarvis.services.insights import collect
        from jarvis.services.intelligence_schedule import (
            telemetry as schedule_telemetry,
        )
        from jarvis.services.roles import REGISTRY

        roles = [{'role_id': role.id, 'title': role.title, 'model': role.model,
                  'purpose': role.purpose, 'consent_scope': role.consent_scope,
                  'max_turns': role.max_turns, 'ceiling_rub': role.max_cost_rub,
                  'tools': list(role.tools)} for role in REGISTRY.values()]
        budget = {'status': 'unavailable', 'global': {'hard_monthly_limit_rub': '500',
            'settled_month': '0', 'unresolved_reserves': '0', 'remaining_rub': '500',
            'states': {'unknown': 0}, 'by_model_rub': {}, 'by_task_rub': {}},
            'purposes': {}, 'roles': {}}
        journal = None
        if service.runtime is not None:
            journal = service.runtime.journal
        else:
            from jarvis.core.llm.budget_store import BudgetStore
            from jarvis.core.llm.governance import canonical_journal
            path = canonical_journal()
            if path.exists():
                journal = BudgetStore(path, currency='RUB')
        if journal is not None:
            budget = budget_telemetry(journal)
            budget['role_spend'] = journal.role_spend(now=datetime.now(UTC))
        schedule = schedule_telemetry(web_only=bool(getattr(app.state, 'web_only', True)))
        insights = collect(service.cfo, service.store)
        report = monthly_report(service.cfo, datetime.now(UTC).date())
        charts = []
        for kind in ('category_breakdown', 'period_comparison', 'category_waterfall'):
            try:
                charts.append(chart_from_report(report, kind))
            except ValueError:
                continue
        return {'models': roles, 'budget': budget, 'schedule': schedule,
                'insights': insights, 'charts': charts,
                'forecast': forecast(service.cfo),
                'financial_apply': False, 'scheduled_dispatch': False}

    @app.post("/api/cognitive/insights/{ident}/feedback", dependencies=[Depends(authorized)])
    def insight_feedback(ident: str, body: InsightFeedback) -> dict[str, Any]:
        from jarvis.services.insights import feedback
        try:
            return feedback(service.store, ident, body.action, note=body.note,
                            remind_minutes=body.remind_minutes)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/api/cognitive/diagnostics", dependencies=[Depends(authorized)])
    async def diagnostics() -> dict[str, Any]:
        policy = service.consent()
        if not policy.maintenance_diagnostics:
            raise HTTPException(403, "Диагностика не включена владельцем")
        data, _, _ = await service.execute("maintenance_diagnostics", {}, policy)
        return {
            "diagnostics": data,
            "patch_application": False,
            "owner_review_required": True,
        }

    @app.post(
        "/api/cognitive/maintenance-proposals", dependencies=[Depends(authorized)]
    )
    def propose(body: MaintenanceProposal) -> dict[str, Any]:
        if not service.consent().maintenance_diagnostics:
            raise HTTPException(403, "Capability not enabled")
        screened(body.title + " " + body.hypothesis + " " + body.suggested_test)
        ident = uuid4().hex
        value = {
            **body.model_dump(exclude={"confirmed"}),
            "id": ident,
            "state": "AWAITING_ENGINEER_TEST_AND_OWNER_APPROVAL",
            "scope": "separate_synthetic_worktree",
            "created_at":datetime.now(UTC).isoformat(),
            "production_shell": False,
            "automatic_apply": False,
            "forbidden": [
                "production_DB",
                "credentials",
                "security_middleware",
                "no-jobs",
                "budget_increase",
                "runtime_HEAD",
            ],
        }
        service.put("cognitive:maintenance:" + ident, value)
        return value

    async def close() -> None:
        for ident in list(service.pending):
            await service.cancel(ident)

    app.router.add_event_handler("shutdown", close)
