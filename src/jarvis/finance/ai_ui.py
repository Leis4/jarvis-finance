"""Owner Qwen chat routes: sessions, streaming reveal, shared memory, read-only tools."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from jarvis.finance.ai_chat import MODELS, ChatStore, QwenChat, memory_candidates
from jarvis.finance.personal_cfo import PersonalCFO

SECRET = re.compile(
    r"(?i)\b(pin|otp|cookie|authorization|password|пароль|пин|cvv|токен)\b|sk-[\w-]+|(?:\d[ -]?){16,20}")


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=80)
    model: str = Field(default="qwen3.7-plus")


class SessionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=80)
    archived: bool | None = None
    model: str | None = None


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=3, max_length=400)
    kind: Literal["preference", "fact", "goal", "constraint"] = "preference"


class MemoryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["confirmed", "rejected", "pending"]


class JournalSettlement(BaseModel):
    """Owner decision about one unresolved AI reservation; never automatic."""
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False
    actual_rub: str | None = Field(default=None, max_length=20)
    note: str = Field(default="", max_length=300)


class AutopilotSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classify: bool | None = None
    budget: bool | None = None
    classify_limit: int | None = Field(default=None, ge=1, le=5)


class BudgetPlanApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    categories: list[dict[str, Any]] = Field(min_length=1, max_length=40)


def _event(name: str, payload: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _qwen_diagnostic(stage: str, payload: dict[str, Any]) -> None:
    """Local, secret-free AI diagnostics: no prompts, no answers, no bank data."""
    try:
        import os
        from pathlib import Path as _Path

        root = _Path("data/cfo-review")
        root.mkdir(parents=True, exist_ok=True)
        path = root / "qwen-diagnostics.jsonl"
        entry = {"at": datetime.now(UTC).isoformat(), "stage": stage, **payload}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + os.linesep)
        if path.stat().st_size > 64_000:
            lines = path.read_text(encoding="utf-8").splitlines()[-200:]
            path.write_text(os.linesep.join(lines) + os.linesep, encoding="utf-8")
    except (OSError, ValueError):
        pass


def _open_reservations() -> list[dict[str, Any]]:
    """Read-only view of journal rows that block paid dispatch; no writes."""
    import sqlite3

    from jarvis.core.llm.budget_store import BudgetStore
    from jarvis.core.llm.governance import canonical_journal
    try:
        store = BudgetStore(canonical_journal(), currency="RUB")
        uri = "file:" + store.path.as_posix() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            rows = conn.execute(
                f"SELECT request_id,status,{store.reserved},created_at FROM {store.reservations} "
                "WHERE status IN ('attempted','uncertain') ORDER BY created_at DESC LIMIT 5").fetchall()
        return [{"request_id": row[0], "status": row[1], "reserved_rub": row[2],
                 "created_at": row[3]} for row in rows]
    except (sqlite3.Error, OSError, ValueError, RuntimeError, AttributeError):
        return []


def install_ai_routes(app: FastAPI, cfo: PersonalCFO, authorized: Callable[[Request], None]) -> None:
    """Install the owner chat surface next to the existing personal routes."""
    store = ChatStore(cfo)
    chat = QwenChat(cfo, store)

    def budget() -> dict[str, Any]:
        from jarvis.core.llm.budget_store import BudgetStore
        from jarvis.core.llm.governance import canonical_journal
        try:
            journal = BudgetStore(canonical_journal(), currency="RUB")
            if not journal.path.is_file():
                return {"status": "not_initialized"}
            return {"status": "available", **journal.global_report(now=datetime.now(UTC))}
        except (RuntimeError, OSError, ValueError):
            return {"status": "unavailable"}

    @app.get("/api/qwen/status", dependencies=[Depends(authorized)])
    def status() -> dict[str, Any]:
        confirmed = store.memory(status="confirmed")
        pending = store.memory(status="pending")
        report = budget()
        return {
            "status": "available",
            "models": list(MODELS),
            "default_model": "qwen3.7-plus",
            "provider": "AITunnel · Qwen",
            "streaming": "progressive_reveal",
            "tools": [tool["function"]["name"] for tool in chat.tools()],
            "memory": {"confirmed": len(confirmed), "pending": len(pending)},
            "budget": report,
            "dispatch_allowed": bool(report.get("dispatch_allowed", False)),
            "open_reservations": _open_reservations(),
            "financial_apply": False,
            "secrets_in_browser": False,
        }

    @app.get("/api/ai/autopilot", dependencies=[Depends(authorized)])
    def autopilot_status() -> dict[str, Any]:
        from jarvis.finance.autopilot import load_settings
        return {"status": "available", "settings": load_settings(cfo),
                "last_run": cfo.db.get_setting("cfo.qwen.last_autopilot", None)}

    @app.put("/api/ai/autopilot", dependencies=[Depends(authorized)])
    def autopilot_update(body: AutopilotSettings) -> dict[str, Any]:
        from jarvis.finance.autopilot import save_settings
        return {"status": "saved", "settings": save_settings(cfo, body.model_dump(exclude_none=True))}

    @app.post("/api/ai/autopilot/classify", dependencies=[Depends(authorized)])
    async def autopilot_classify(body: AutopilotSettings | None = None) -> dict[str, Any]:
        from jarvis.finance.autopilot import classify_pending
        limit = body.classify_limit if body else None
        return await classify_pending(cfo, limit=limit)

    @app.get("/api/cfo/budget-plan", dependencies=[Depends(authorized)])
    def cfo_budget_plan(goal_id: int | None = None) -> dict[str, Any]:
        from jarvis.finance.autopilot import budget_plan
        return budget_plan(cfo, goal_id=goal_id)

    @app.post("/api/cfo/budget-plan/apply", dependencies=[Depends(authorized)])
    def cfo_budget_apply(body: BudgetPlanApply) -> dict[str, Any]:
        from jarvis.finance.autopilot import apply_budget_plan
        return apply_budget_plan(cfo, body.categories)

    @app.get("/api/ai/sessions", dependencies=[Depends(authorized)])
    def sessions(include_archived: bool = False) -> dict[str, Any]:
        return {"status": "available", "items": store.sessions(include_archived=include_archived)}

    @app.post("/api/ai/sessions", dependencies=[Depends(authorized)])
    def create_session(body: SessionInput | None = None) -> dict[str, Any]:
        payload = body or SessionInput()
        if payload.model not in MODELS:
            raise HTTPException(422, "Неизвестная модель")
        return {"status": "saved", "session": store.create(payload.title, payload.model)}

    @app.patch("/api/ai/sessions/{ident}", dependencies=[Depends(authorized)])
    def patch_session(ident: str, body: SessionPatch) -> dict[str, Any]:
        if store.session(ident) is None:
            raise HTTPException(404, "Чат не найден")
        try:
            store.update(ident, title=body.title, archived=body.archived, model=body.model)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"status": "saved", "session": store.session(ident)}

    @app.delete("/api/ai/sessions/{ident}", dependencies=[Depends(authorized)])
    def delete_session(ident: str) -> dict[str, Any]:
        return {"status": "deleted" if store.delete(ident) else "not_found"}

    @app.get("/api/ai/sessions/{ident}/messages", dependencies=[Depends(authorized)])
    def messages(ident: str) -> dict[str, Any]:
        session = store.session(ident)
        if session is None:
            raise HTTPException(404, "Чат не найден")
        return {"status": "available", "session": session, "items": store.messages(ident)}

    @app.get("/api/ai/search", dependencies=[Depends(authorized)])
    def search(q: str = Query(min_length=1, max_length=120)) -> dict[str, Any]:
        return {"status": "available", "query": q, "items": store.search(q)}

    @app.get("/api/ai/memory", dependencies=[Depends(authorized)])
    def memory(status_filter: str | None = None) -> dict[str, Any]:
        return {"status": "available", "items": store.memory(status=status_filter)}

    @app.post("/api/ai/memory", dependencies=[Depends(authorized)])
    def add_memory(body: MemoryInput) -> dict[str, Any]:
        try:
            item = store.remember(body.text, kind=body.kind, status="confirmed", source="owner")
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"status": "saved", "item": item}

    @app.post("/api/ai/memory/{ident}/decide", dependencies=[Depends(authorized)])
    def decide_memory(ident: str, body: MemoryDecision) -> dict[str, Any]:
        try:
            saved = store.decide_memory(ident, body.status)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"status": "saved" if saved else "not_found"}

    @app.delete("/api/ai/memory/{ident}", dependencies=[Depends(authorized)])
    def forget(ident: str) -> dict[str, Any]:
        return {"status": "deleted" if store.forget(ident) else "not_found"}

    @app.get("/api/qwen/journal", dependencies=[Depends(authorized)])
    def journal() -> dict[str, Any]:
        """Read-only view of governed AI rows that block or shape dispatch."""
        return {"status": "available", "open": _open_reservations(), "budget": budget()}

    @app.post("/api/qwen/journal/{request_id}/settle", dependencies=[Depends(authorized)])
    def settle_journal(request_id: str, body: JournalSettlement) -> dict[str, Any]:
        """Owner-confirmed worst-case settlement; never automatic, never a release."""
        import hashlib
        import sqlite3
        from decimal import Decimal as _Decimal

        from jarvis.core.llm.budget_store import BudgetStore
        from jarvis.core.llm.governance import canonical_journal
        if not body.confirm:
            raise HTTPException(422, "Нужно явное подтверждение владельца")
        if len(body.note.strip()) < 10:
            raise HTTPException(422, "Опишите основание сверки (минимум 10 символов)")
        open_rows = {row["request_id"]: row for row in _open_reservations()}
        row = open_rows.get(request_id)
        if row is None:
            raise HTTPException(404, "Открытая запись журнала не найдена")
        reserved = _Decimal(str(row["reserved_rub"]))
        amount = _Decimal(body.actual_rub.replace(",", ".")) if body.actual_rub else reserved
        if amount < 0 or amount > reserved:
            raise HTTPException(422, "Сумма должна быть от 0 до зарезервированной")
        stamp = datetime.now(UTC).isoformat()
        evidence = hashlib.sha256(
            f"owner-journal-settlement|{request_id}|{amount}|{body.note.strip()}|{stamp}".encode()).hexdigest()
        try:
            store = BudgetStore(canonical_journal(), currency="RUB")
            store.settle(request_id, amount, now=datetime.now(UTC),
                         resolution_evidence_sha256=evidence)
        except sqlite3.Error as error:
            raise HTTPException(503, "Журнал недоступен") from error
        except (RuntimeError, ValueError) as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "settled", "request_id": request_id, "amount_rub": str(amount),
                "evidence_sha256": evidence, "budget": budget()}

    @app.post("/api/ai/sessions/{ident}/message", dependencies=[Depends(authorized)])
    async def send(ident: str, body: MessageInput) -> StreamingResponse:
        session = store.session(ident)
        if session is None:
            raise HTTPException(404, "Чат не найден")
        text = body.text.strip()
        if SECRET.search(text):
            raise HTTPException(422, "Не отправляйте PIN, пароли, куки и номера карт")
        store.add_message(ident, "user", text)
        first_message = not any(m["role"] == "assistant" for m in store.messages(ident, limit=10))

        async def stream() -> AsyncIterator[bytes]:
            yield _event("meta", {"session_id": ident, "model": session["model"],
                                  "started_at": datetime.now(UTC).isoformat()})
            yield _event("status", {"text": "думает"})
            try:
                result = await asyncio.wait_for(chat.turn(ident, text), timeout=150)
            except TimeoutError:
                _qwen_diagnostic("timeout", {"model": session["model"]})
                yield _event("error", {"message": "Провайдер не ответил за 150 с", "code": "timeout"})
                return
            except Exception as error:  # noqa: BLE001 - never break the stream
                _qwen_diagnostic("exception", {"model": session["model"],
                                               "error": type(error).__name__})
                yield _event("error", {
                    "message": "Не удалось получить ответ от AI. Бесплатный режим «Расчёт» работает.",
                    "code": type(error).__name__})
                return
            for item in result.get("events", []):
                yield _event("tool", item)
            if result.get("status") != "ok":
                code = result.get("code")
                messages = {
                    "journal_unknown_attempt": "AI ждёт сверки журнала: есть неизвестная попытка списания. "
                                               "Локальные расчёты и очередь категорий работают.",
                    "dispatch_blocked": "Платный AI приостановлен политикой журнала расходов.",
                    "provider_unavailable": "Провайдер AI недоступен. Локальные расчёты работают.",
                }
                error_text = messages.get(code, "Месячный лимит AI исчерпан" if result.get("status") == "quota"
                                    else "Qwen сейчас недоступен")
                _qwen_diagnostic("error", {"model": session["model"], "code": code,
                                           "status": result.get("status")})
                try:
                    store.add_message(ident, "assistant", error_text, meta={"error": code})
                except sqlite3.Error:
                    pass
                yield _event("error", {"message": error_text, "code": code})
                return
            content = result["text"]
            _qwen_diagnostic("answer", {"model": result.get("model"), "chars": len(content),
                                        "tokens_in": result.get("tokens_in"),
                                        "tokens_out": result.get("tokens_out"),
                                        "tools": [e.get("name") for e in result.get("events", [])
                                                  if e.get("role") == "tool"],
                                        "cost_rub": result.get("paid_cost_rub")})
            try:
                store.add_message(ident, "assistant", content, model=result.get("model"),
                    tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"),
                    cost_rub=str(result.get("paid_cost_rub") or "0"),
                    meta={"events": result.get("events", [])})
                store.touch(ident, cost_rub=str(Decimal_total(session.get("spent_rub"),
                                                              result.get("paid_cost_rub"))))
                if first_message:
                    store.update(ident, title=" ".join(text.split())[:60])
            except sqlite3.Error:
                # The answer is still delivered; the next turn rebuilds history.
                pass
            # Fixed-width slices keep newlines and formatting intact.
            for index in range(0, len(content), 40):
                yield _event("text", {"chunk": content[index:index + 40]})
                await asyncio.sleep(0.012)
            suggestions: list[dict[str, Any]] = []
            try:
                for candidate in memory_candidates(text):
                    if all(candidate != item["text"] for item in store.memory()):
                        suggestions.append(store.remember(candidate, status="pending", source="qwen"))
            except sqlite3.Error:
                suggestions = []
            yield _event("done", {
                "paid_cost_rub": str(result.get("paid_cost_rub") or "0"),
                "model": result.get("model"), "tools": result.get("events", []),
                "actions": result.get("actions", []),
                "memory_suggestions": suggestions, "text": content,
                "session": store.session(ident),
            })

        return StreamingResponse(stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def Decimal_total(current: Any, delta: Any) -> str:
    """Sum session spend without float drift."""
    from decimal import Decimal
    total = Decimal(str(current or "0")) + Decimal(str(delta or "0"))
    return str(total.quantize(Decimal("0.01")))
