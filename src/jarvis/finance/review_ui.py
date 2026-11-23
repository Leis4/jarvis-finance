"""Loopback-only browser review; separate from production API and database."""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import sqlite3
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import ceil, isfinite
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from jarvis.finance.audit_report import AcceptedStatementScope
from jarvis.finance.classification.teacher import TeacherPermissions, TeacherPolicy
from jarvis.finance.review_store import ReviewStore
from jarvis.services.chat_scenarios import ChatScenario

if TYPE_CHECKING:
    from jarvis.finance.personal_cfo import PersonalCFO
    from jarvis.finance.scheduler.agent import AgentRuntime
    from jarvis.knowledge.retriever import KnowledgeRetriever


class PublicQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)


class FinancialQuestion(BaseModel):
    question: str | None = Field(default=None, min_length=1, max_length=500)
    model_config = ConfigDict(extra="forbid")
    intent: Literal["spending", "expense_history", "category_growth", "recurring", "duplicates", "missing_data", "stale_balances", "available_funds", "bank_movement", "income", "food"]
    month: str = Field(default='', pattern=r"^(?:|\d{4}-\d{2})$")
    statement_scope: AcceptedStatementScope | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$", default="RUB")

    @model_validator(mode='after')
    def explicit_authority(self) -> FinancialQuestion:
        if (self.statement_scope is None and not self.month) or (self.statement_scope is not None and self.month):
            raise ValueError('Choose one explicit monthly ledger or narrow statement scope')
        return self


class InsightScan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    currency: str = Field(pattern=r"^[A-Z]{3}$", default="RUB")


class LocalAnalysisRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    currency: Literal["RUB", "USD", "EUR"] = "RUB"


class QwenPilotRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    currency: Literal["RUB"] = "RUB"
    preview_binding: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    owner_confirmed: bool = Field(default=False, strict=True)


class FinancialAnalysisRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    public_task_id: int = Field(ge=1, strict=True)


class InsightDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: int = Field(ge=1, strict=True)
    card_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["acknowledge", "dismiss", "reopen"]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    currency: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    public_cloud: bool = False
    explain: bool = False
    scenario: ChatScenario | None = None


class MemoryNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["preference", "hypothesis", "prediction"]
    text: str = Field(min_length=1, max_length=500)
    actor: str = Field(min_length=1, max_length=100)
    model_version: str | None = Field(default=None, max_length=100)
    expires_at: datetime | None = None


class MemoryRevocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)


class Decision(BaseModel):
    action: str
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    candidate_id: int | None = None


class PasskeyRegistration(BaseModel):
    """Browser registration payload; verified by py-webauthn on the server."""
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    credential: dict[str, Any]
    label: str = Field(default="Face ID", max_length=60)


class PasskeyAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    credential: dict[str, Any]


class QueueFilter(BaseModel):
    model_config = {"extra": "forbid"}
    classification: str = ""
    reason: str = ""
    offset: int = Field(0, ge=0)
    status: Literal["", "pending_review", "confirm_existing", "approve_new", "reject"] = ""
    search: str = Field(default="", max_length=200)
    card: str = Field(default="", pattern=r"^(?:|[0-9]{4})$")
    start: date | None = None
    end: date | None = None
    direction: Literal["", "debit", "credit", "zero", "unknown"] = ""


def _matches_queue_filter(item: dict[str, Any], filters: QueueFilter) -> bool:
    """Filter retained observations, never infer income/category or native sign meaning."""
    operation = item["operation"]
    values = [operation.get(key) for key in ("description", "merchant", "comment", "payee")]
    text = " ".join(v for v in values if isinstance(v, str)).casefold()
    if filters.search and filters.search.strip().casefold() not in text:
        return False
    if filters.card and re.sub(r"\D", "", str(item.get("statement_card") or "")) != filters.card:
        return False
    if filters.start or filters.end:
        try:
            observed = date.fromisoformat(operation["tx_date"])
        except (KeyError, TypeError, ValueError):
            return False
        if (filters.start and observed < filters.start) or (filters.end and observed > filters.end):
            return False
    if filters.direction:
        direction = "unknown"
        try:
            raw = operation.get("amount")
            if item.get("source_kind") in {"sber_mcp", "generated_pdf"} or isinstance(raw, bool) or len(str(raw)) > 64:
                raise ValueError
            amount = Decimal(str(raw))
            if not amount.is_finite():
                raise ValueError
            direction = "debit" if amount < 0 else "credit" if amount > 0 else "zero"
        except (ValueError, InvalidOperation):
            pass
        if direction != filters.direction:
            return False
    return True


class CapacityRequest(BaseModel):
    fact_ids: dict[str, str]
    start: date
    end: date
    currency: str = Field(pattern="^[A-Z]{3}$")
    scenarios: dict[str, Decimal]


class LabelDecision(BaseModel):
    action: str
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    category: str | None = None


class GroupDecision(BaseModel):
    group_hash: str
    action: str
    actor: str
    reason: str
    confirmed: bool = False
    candidate_id: int | None = None


class TeacherGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: TeacherPolicy
    actor: str = Field(min_length=1, max_length=100)
    confirmed: bool = Field(strict=True)


class OwnerAIActivation(TeacherGrant):
    action_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class TeacherAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permission_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class TeacherRevoke(TeacherAction):
    actor: str = Field(min_length=1, max_length=100)


class TeacherProposal(TeacherAction):
    expected_binding: str = Field(pattern=r"^[0-9a-f]{64}$")


PAGE = (Path(__file__).with_name("review_assets") / "index.html").read_text(encoding="utf-8")
for _marker, _asset in [("FRONTEND_STYLE", "app.css"), ("FRONTEND_SCRIPT", "review.js")]:
    PAGE = PAGE.replace(_marker, (Path(__file__).with_name("review_assets") / _asset).read_text(encoding="utf-8"))



def create_review_app(store: ReviewStore, token: str, *, max_requests: int = 120,
                      request_window_seconds: float = 60, ledger_path: Path | None = None,
                      accepted_sources: frozenset[str] = frozenset(), accepted_sha256: str | None = None,
                      knowledge_retriever: KnowledgeRetriever | None = None,
                      knowledge_sources: frozenset[str] = frozenset(), ai_budget_path: Path | None = None,
                      ai_pricing_path: Path | None = None, ai_mock_transport: Any = None,
                      agent_runtime: AgentRuntime | None = None, public_search_transport: Any = None,
                      personal_cfo: PersonalCFO | None = None, web_only: bool = False) -> FastAPI:
    """Serve a private queue with loopback, host, origin and CSRF guards."""
    if len(token) < 32:
        raise ValueError("A strong local review token is required")
    if max_requests < 1 or not isfinite(request_window_seconds) or request_window_seconds <= 0:
        raise ValueError("Positive finite local request limits required")
    ai_observations: dict[str, Any] = {"local_questions": 0, "source_error": None}
    # Remembered devices, pairings and passkeys live in one private SQLite file.
    from jarvis.finance.owner_devices import (
        PAIR_TTL_SECONDS,
        OwnerAuthStore,
        allowed_origins,
        authentication_options,
        registration_options,
        rp_id_for,
        verify_authentication,
        verify_registration,
    )
    from jarvis.finance.review_auth import (
        DEVICE_COOKIE,
        DEVICE_TTL_DAYS,
        OwnerAuth,
        _public_origin,
        allowed_hosts,
        origin_candidates,
        web_only_allowed,
    )
    try:
        owner_store = OwnerAuthStore(store.path.parent / 'owner-auth.sqlite',
                                     public_origin=_public_origin())
    except (OSError, ValueError):
        owner_store = None
    RP_ID = rp_id_for(_public_origin())
    RP_ORIGINS = allowed_origins(_public_origin())
    owner = OwnerAuth(token, owner_store)
    if agent_runtime is not None and agent_runtime.store.path != store.path:
        raise ValueError("Agent results require the same review workspace")

    def local(request: Request) -> None:
        public = _public_origin()
        host = request.headers.get("host", "")
        hostname = urlsplit("http://" + host).hostname
        client_host = request.client.host if request.client else ""
        try:
            loopback = bool(client_host) and ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            loopback = False
        trusted_front_door = bool(public) and hostname == public[8:]
        if not loopback and not trusted_front_door:
            raise HTTPException(403, "Loopback review only")
        if hostname not in allowed_hosts():
            raise HTTPException(403, "Untrusted review host")
        origin = request.headers.get("origin")
        if origin and origin not in origin_candidates(host):
            raise HTTPException(403, "Cross-origin review denied")
        if request.headers.get('sec-fetch-site') in {'cross-site', 'same-site'}:
            raise HTTPException(403, 'Cross-site review denied')

    def authorized(request: Request) -> None:
        local(request)
        owner.authorize(request)

    def _is_https(request: Request) -> bool:
        return request.headers.get('x-forwarded-proto', '').lower() == 'https'

    login_failures: dict[str, deque[float]] = {}
    login_window_seconds = 300.0
    login_max_attempts = 8

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web_only = web_only

    @app.post('/auth/session')
    async def owner_login(request: Request) -> Response:
        owner.same_origin(request)
        client_key = request.client.host if request.client else "unknown"
        now = monotonic()
        attempts = login_failures.setdefault(client_key, deque())
        while attempts and attempts[0] <= now - login_window_seconds:
            attempts.popleft()
        if len(attempts) >= login_max_attempts:
            retry = max(1, ceil(login_window_seconds - (now - attempts[0])))
            return JSONResponse({'detail': 'Слишком много попыток входа. Подождите и повторите.'},
                status_code=429, headers={'Retry-After': str(retry)})
        body = b''
        async for chunk in request.stream():
            body += chunk
            if len(body) > 2048:
                raise HTTPException(413, 'Owner login body too large')
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Invalid owner login') from None
        if not isinstance(value, dict) or not isinstance(value.get('key'), str) or not owner.matches(value['key']):
            attempts.append(now)
            if len(login_failures) > 256:
                login_failures.pop(next(iter(login_failures)), None)
            raise HTTPException(403, 'Owner login denied')
        attempts.clear()
        remember = bool(value.get('remember'))
        device_id = None
        response = JSONResponse({'status':'signed_in'})
        if remember and owner_store is not None:
            label = 'Устройство'
            agent = request.headers.get('user-agent', '')
            if 'iPhone' in agent:
                label = 'iPhone'
            elif 'iPad' in agent:
                label = 'iPad'
            elif 'Windows' in agent:
                label = 'Windows'
            device, device_cookie = owner_store.create_device(
                label, user_agent=agent, ttl_days=DEVICE_TTL_DAYS)
            device_id = device['id']
            response.set_cookie(DEVICE_COOKIE, device_cookie,
                max_age=DEVICE_TTL_DAYS * 24 * 60 * 60, httponly=True, samesite='strict',
                path='/', secure=_is_https(request))
        owner.sign_in(response, secure=_is_https(request), device_id=device_id)
        return response

    @app.post('/auth/device')
    def device_sign_in(request: Request) -> Response:
        """Restore an owner session from a remembered device (no key replay)."""
        owner.same_origin(request)
        if owner_store is None:
            raise HTTPException(503, 'Устройства недоступны')
        device = owner_store.find_device(request.cookies.get(DEVICE_COOKIE, ''))
        if device is None:
            raise HTTPException(401, 'Нет запомненного устройства')
        response = JSONResponse({'status': 'signed_in', 'device': {
            'id': device['id'], 'label': device['label'], 'expires_at': device['expires_at']}})
        owner.sign_in(response, secure=_is_https(request), device_id=device['id'])
        return response

    @app.get('/auth/pair/{token}')
    def pair_status(token: str, request: Request) -> dict[str, Any]:
        if owner_store is None:
            raise HTTPException(503, 'Устройства недоступны')
        row = owner_store.get_challenge(token)
        if row is None or row['kind'] != 'pair':
            raise HTTPException(404, 'Запрос не найден')
        owner_store.mark_opened(token)
        approved = bool(row['approved']) and not row['used']
        return {'status': 'approved' if approved else 'pending',
                'expires_at': row['expires_at'], 'used': bool(row['used'])}

    @app.get('/pair/{token}', response_class=HTMLResponse)
    def pair_page(token: str, request: Request) -> HTMLResponse:
        """Phone-side pairing page; no secrets in the URL."""
        nonce = secrets.token_urlsafe(24)
        page = (Path(__file__).with_name('review_assets') / 'pair.html').read_text(encoding='utf-8')
        page = page.replace('<style>', f'<style nonce="{nonce}">').replace(
            '<script>', f'<script nonce="{nonce}">')
        policy = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                  "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                  "frame-ancestors 'none'; form-action 'none'")
        return HTMLResponse(page, headers={"Content-Security-Policy": policy,
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.post('/auth/pair/{token}/claim')
    def pair_claim(token: str, request: Request) -> Response:
        owner.same_origin(request)
        if owner_store is None:
            raise HTTPException(503, 'Устройства недоступны')
        row = owner_store.get_challenge(token)
        if row is None or row['kind'] != 'pair' or row['used']:
            raise HTTPException(410, 'Запрос недействителен')
        if not row['approved']:
            raise HTTPException(409, 'Сначала подтвердите устройство на компьютере')
        consumed = owner_store.consume(token, 'pair')
        if consumed is None:
            raise HTTPException(410, 'Запрос истёк')
        device, device_cookie = owner_store.create_device(
            'iPhone', user_agent=request.headers.get('user-agent', ''), ttl_days=DEVICE_TTL_DAYS)
        response = JSONResponse({'status': 'paired', 'device': {
            'id': device['id'], 'label': device['label'], 'expires_at': device['expires_at']}})
        response.set_cookie(DEVICE_COOKIE, device_cookie,
            max_age=DEVICE_TTL_DAYS * 24 * 60 * 60, httponly=True, samesite='strict',
            path='/', secure=_is_https(request))
        owner.sign_in(response, secure=_is_https(request), device_id=device['id'])
        return response

    @app.post('/api/auth/pair/start', dependencies=[Depends(authorized)])
    def pair_start() -> dict[str, Any]:
        if owner_store is None:
            raise HTTPException(503, 'Устройства недоступны')
        challenge = owner_store.new_challenge('pair', ttl=PAIR_TTL_SECONDS)
        base = _public_origin() or 'http://127.0.0.1:8766'
        url = f"{base}/pair/{challenge['id']}"
        import io

        import qrcode
        from qrcode.image.svg import SvgPathImage

        image = qrcode.make(url, image_factory=SvgPathImage)
        buffer = io.BytesIO()
        image.save(buffer)
        svg = buffer.getvalue().decode('utf-8')
        if svg.startswith('<?xml'):
            svg = svg.split('?>', 1)[-1].lstrip()
        return {'status': 'pending', 'id': challenge['id'], 'url': url,
                'expires_at': challenge['expires_at'], 'svg': svg}

    @app.get('/api/auth/pair/{token}', dependencies=[Depends(authorized)])
    def pair_watch(token: str) -> dict[str, Any]:
        if owner_store is None:
            raise HTTPException(503, 'Устройства недоступны')
        row = owner_store.get_challenge(token)
        if row is None:
            raise HTTPException(404, 'Запрос не найден')
        return {'status': 'approved' if row['approved'] else 'pending',
                'opened': bool(row['opened']), 'expires_at': row['expires_at'],
                'used': bool(row['used'])}

    @app.post('/api/auth/pair/{token}/approve', dependencies=[Depends(authorized)])
    def pair_approve(token: str, request: Request) -> dict[str, Any]:
        if owner_store is None:
            raise HTTPException(503, 'Устройства недоступны')
        if not owner_store.approve(token, approved_by='owner-session'):
            raise HTTPException(409, 'Запрос уже истёк или не найден')
        return {'status': 'approved'}

    @app.get('/api/auth/devices', dependencies=[Depends(authorized)])
    def devices() -> dict[str, Any]:
        if owner_store is None:
            return {'status': 'unavailable', 'items': []}
        return {'status': 'available', 'items': owner_store.list_devices(),
                'passkeys': owner_store.list_credentials(),
                'ttl_days': DEVICE_TTL_DAYS}

    @app.post('/api/auth/devices/{ident}/revoke', dependencies=[Depends(authorized)])
    def revoke_device(ident: str, request: Request) -> Response:
        if owner_store is None:
            raise HTTPException(503, 'Устройства недоступны')
        if not re.fullmatch(r'[0-9a-f]{32}', ident):
            raise HTTPException(422, 'Некорректный идентификатор')
        owner_store.revoke_device(ident)
        response = JSONResponse({'status': 'revoked'})
        if request.cookies.get(DEVICE_COOKIE, '').startswith(ident + '.'):
            response.delete_cookie(DEVICE_COOKIE, path='/')
            owner.sign_out(request, response)
        return response

    @app.post('/api/auth/logout-all', dependencies=[Depends(authorized)])
    def logout_all(request: Request) -> Response:
        if owner_store is None:
            raise HTTPException(503, 'Устройства недоступны')
        count = owner_store.revoke_all()
        response = JSONResponse({'status': 'all_signed_out', 'devices': count})
        response.delete_cookie(DEVICE_COOKIE, path='/')
        owner.sign_out(request, response)
        return response

    @app.post('/api/auth/passkey/register/begin', dependencies=[Depends(authorized)])
    def passkey_begin() -> dict[str, Any]:
        if owner_store is None:
            raise HTTPException(503, 'Passkeys недоступны')
        options, challenge_id = registration_options(owner_store, rp_id=RP_ID, label='Face ID')
        return {'status': 'ok', 'challenge_id': challenge_id, 'options': options}

    @app.post('/api/auth/passkey/register/complete', dependencies=[Depends(authorized)])
    def passkey_complete(body: PasskeyRegistration, request: Request) -> Response:
        if owner_store is None:
            raise HTTPException(503, 'Passkeys недоступны')
        device = owner_store.find_device(request.cookies.get(DEVICE_COOKIE, ''))
        device_id = device['id'] if device else None
        try:
            result = verify_registration(owner_store, body.challenge_id, body.credential,
                rp_id=RP_ID, origins=RP_ORIGINS, device_id=device_id, label=body.label or 'Face ID')
        except Exception:  # noqa: BLE001 - library errors must not leak details
            raise HTTPException(400, 'Регистрация не подтверждена') from None
        fresh_cookie = None
        if device_id is None:
            created, fresh_cookie = owner_store.create_device('iPhone', user_agent=request.headers.get(
                'user-agent', ''), ttl_days=DEVICE_TTL_DAYS)
            device_id = created['id']
            owner_store.attach_credential(result['credential_id'], device_id)
        response = JSONResponse({'status': 'registered', 'credential_id': result['credential_id'],
                                 'device_id': device_id})
        if fresh_cookie:
            response.set_cookie(DEVICE_COOKIE, fresh_cookie,
                max_age=DEVICE_TTL_DAYS * 24 * 60 * 60, httponly=True, samesite='strict',
                path='/', secure=_is_https(request))
        return response

    @app.post('/auth/passkey/login/begin')
    def passkey_login_begin(request: Request) -> dict[str, Any]:
        owner.same_origin(request)
        if owner_store is None or not owner_store.list_credentials():
            raise HTTPException(404, 'Passkeys не зарегистрированы')
        options, challenge_id = authentication_options(owner_store, rp_id=RP_ID)
        return {'status': 'ok', 'challenge_id': challenge_id, 'options': options}

    @app.post('/auth/passkey/login/complete')
    def passkey_login_complete(body: PasskeyAssertion, request: Request) -> Response:
        owner.same_origin(request)
        if owner_store is None:
            raise HTTPException(503, 'Passkeys недоступны')
        try:
            result = verify_authentication(owner_store, body.challenge_id, body.credential,
                rp_id=RP_ID, origins=RP_ORIGINS)
        except Exception:  # noqa: BLE001
            raise HTTPException(401, 'Face ID не подтверждён') from None
        device = owner_store.find_device(request.cookies.get(DEVICE_COOKIE, ''))
        device_id = result.get('device_id') or (device['id'] if device else None)
        response = JSONResponse({'status': 'signed_in', 'device_id': device_id})
        owner.sign_in(response, secure=_is_https(request), device_id=device_id)
        return response

    @app.get('/auth/csrf')
    def owner_csrf(request: Request) -> dict[str, Any]:
        session = owner.session(request)
        if session is None:
            raise HTTPException(403, 'Owner session required')
        return {'csrf':session.csrf, 'web_only':web_only}

    @app.post('/auth/logout', dependencies=[Depends(authorized)])
    def owner_logout(request: Request) -> Response:
        response = JSONResponse({'status':'signed_out'})
        owner.sign_out(request, response)
        return response
    if personal_cfo is not None:
        from jarvis.finance.personal_ui import install_personal_routes
        install_personal_routes(app, personal_cfo, authorized)
        from jarvis.finance.ai_ui import install_ai_routes
        install_ai_routes(app, personal_cfo, authorized)
    from jarvis.services.assistant import AssistantService, validate_chat_identity
    assistant = AssistantService(store, ledger=ledger_path, accepted_sources=accepted_sources,
        accepted_sha256=accepted_sha256, knowledge=knowledge_retriever, public_sources=knowledge_sources,
        budget=ai_budget_path, prices=ai_pricing_path, transport=ai_mock_transport, personal_cfo=personal_cfo)
    if personal_cfo is not None:
        from jarvis.finance.cognitive_ui import install_cognitive_routes
        install_cognitive_routes(app,assistant.cognitive_conversation(),authorized)
    from jarvis.finance.research_ui import install_research_routes
    from jarvis.finance.statement_ui import install_statement_routes
    from jarvis.finance.telegram.review_ui import install_telegram_routes
    install_statement_routes(app, store, authorized)
    install_telegram_routes(app, store, authorized)
    public_research_results = install_research_routes(app, authorized, agent_runtime, public_search_transport)

    @app.post("/api/chat", dependencies=[Depends(authorized)])
    async def chat(body: ChatRequest) -> dict[str, Any]:
        return await assistant.ask(**body.model_dump(exclude={"scenario"}), scenario=body.scenario)

    @app.get("/api/chat/{conversation_id}", dependencies=[Depends(authorized)])
    def chat_history(conversation_id: str) -> dict[str, Any]:
        return {"turns": store.chat_history(validate_chat_identity(conversation_id)), "historical": True,
                "production_database_modified": False}
    attempts: deque[float] = deque()

    @app.middleware("http")
    async def private_boundary(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        try:
            local(request)
        except (HTTPException, ValueError) as boundary_error:
            reason = getattr(boundary_error, "detail", None)
            reason = reason if isinstance(reason, str) and len(reason) <= 80 else "boundary_denied"
            response: Response = JSONResponse(status_code=403,
                content={"detail": "Untrusted local review request", "reason": reason})
        else:
            now = monotonic()
            while attempts and attempts[0] <= now - request_window_seconds:
                attempts.popleft()
            if len(attempts) >= max_requests:
                delay = max(1, ceil(request_window_seconds - (now - attempts[0])))
                response = JSONResponse(status_code=429, content={"detail": "Local review request limit; retry later"},
                                        headers={"Retry-After": str(delay)})
            else:
                attempts.append(now)
                try:
                    if request.url.path.startswith('/api/'):
                        authorized(request)
                        from jarvis.finance.cognitive_ui import cognitive_route
                        if web_only and not (web_only_allowed(request.method, request.url.path)
                                or (personal_cfo is not None and cognitive_route(request.method,request.url.path))):
                            raise HTTPException(403, 'Web-only: operation disabled')
                except (HTTPException, ValueError):
                    response = JSONResponse(status_code=403, content={'detail':'Owner authorization or web-only policy denied'})
                else:
                    response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff", 'Vary':'Cookie'})
        return response

    @app.exception_handler(ValueError)
    async def invalid(_: Request, exc: ValueError) -> Any:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    async def unavailable(_: Request, exc: Exception) -> Any:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": "Source unavailable: " + type(exc).__name__})

    app.add_exception_handler(OSError, unavailable)
    app.add_exception_handler(sqlite3.Error, unavailable)
    from jarvis.core.llm.budget_store import (
        BudgetReservationDenied,
        BudgetStoreUnavailable,
    )
    from jarvis.core.llm.errors import LLMError
    app.add_exception_handler(BudgetReservationDenied, unavailable)
    app.add_exception_handler(BudgetStoreUnavailable, unavailable)
    app.add_exception_handler(LLMError, unavailable)

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(local)])
    def index(request: Request) -> HTMLResponse:
        nonce = secrets.token_urlsafe(24)
        assets = Path(__file__).with_name("review_assets")
        content = PAGE
        signed_in = owner.session(request) is not None
        api_owner = owner.matches(request.headers.get('x-review-token', ''))
        if not signed_in and not api_owner:
            content = (assets / 'owner_login.html').read_text(encoding='utf-8')
        elif personal_cfo is not None and request.query_params.get('technical') != '1':
            content = (assets / 'finance.html').read_text(encoding='utf-8')
            content = content.replace('__STYLE__', (assets / 'finance.css').read_text(encoding='utf-8'))
            content = content.replace('__SCRIPT__', (assets / 'finance.js').read_text(encoding='utf-8'))
            content = content.replace('data-web-only="false"',
                'data-web-only="' + str(web_only).lower() + '"')
            content = content.replace('<style>', f'<style nonce="{nonce}">')
            content = content.replace('<script>', f'<script nonce="{nonce}">')
            policy = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                      "connect-src 'self'; img-src 'self' data:; manifest-src 'self'; "
                      "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            return HTMLResponse(content, headers={"Content-Security-Policy": policy})
        if signed_in or api_owner:
            content = content.replace('<script>', '<script>' + (assets/'owner_session.js').read_text(encoding='utf-8'), 1)
            content = content.replace('<html', '<html data-web-only="' + str(web_only).lower() + '"', 1)
        page = content.replace("TOKEN", 'null').replace("<style>", f'<style nonce="{nonce}">').replace("<script>", f'<script nonce="{nonce}">')
        policy = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                  "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        return HTMLResponse(page, headers={"Content-Security-Policy": policy})

    @app.get('/manifest.webmanifest', dependencies=[Depends(local)])
    def manifest() -> JSONResponse:
        return JSONResponse({
            'name': 'JARVIS · личные финансы', 'short_name': 'JARVIS',
            'description': 'Персональный финансовый дашборд владельца',
            'start_url': '/', 'scope': '/', 'display': 'standalone',
            'background_color': '#080a0e', 'theme_color': '#080a0e', 'lang': 'ru',
            'icons': [
                {'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
                {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
                {'src': '/icon.svg', 'sizes': 'any', 'type': 'image/svg+xml'},
            ],
        }, media_type='application/manifest+json')

    @app.get('/icon.svg', dependencies=[Depends(local)])
    def icon_svg() -> Response:
        return Response((Path(__file__).with_name('review_assets') / 'icon.svg').read_bytes(),
            media_type='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400'})

    @app.get('/icon-192.png', dependencies=[Depends(local)])
    @app.get('/icon-180.png', dependencies=[Depends(local)])
    @app.get('/icon-512.png', dependencies=[Depends(local)])
    def icon_png(request: Request) -> Response:
        name = request.url.path.lstrip('/')
        allowed = {'icon-192.png', 'icon-180.png', 'icon-512.png'}
        if name not in allowed:
            raise HTTPException(404, 'Unknown icon')
        return Response((Path(__file__).with_name('review_assets') / name).read_bytes(),
            media_type='image/png', headers={'Cache-Control': 'public, max-age=86400'})

    @app.get("/api/groups", dependencies=[Depends(authorized)])
    def groups() -> dict[str, Any]:
        from jarvis.finance.review_backup import review_snapshot
        with review_snapshot(store) as snapshot:
            packet = json.loads(snapshot.metadata()["packet"])
        groups = packet.get("groups") or []
        return {"reasons": sorted({g["reason"] for g in groups}), "groups": groups}

    @app.get("/api/financial-facts/{fact_id}", dependencies=[Depends(authorized)])
    def financial_fact(fact_id: str) -> dict[str, Any]:
        try:
            fact = store.financial_fact(fact_id)
            return {key: value for key, value in fact.items() if key not in {"source_path", "source_root"}}
        except (OSError, ValueError, KeyError):
            raise HTTPException(409, "Financial source unavailable or changed") from None

    @app.post("/api/financial-facts/{fact_id}/decision", dependencies=[Depends(authorized)])
    def financial_decision(fact_id: str, body: Decision) -> dict[str, Any]:
        if body.candidate_id is not None:
            raise HTTPException(422, "Operation candidates cannot confirm financial facts")
        try:
            return store.decide_financial_fact(fact_id, body.action, actor=body.actor, reason=body.reason,
                                               expected_revision=body.expected_revision)
        except (OSError, ValueError, KeyError):
            raise HTTPException(409, "Invalid, stale or unavailable financial decision") from None

    @app.post("/api/capacity-preview", dependencies=[Depends(authorized)])
    def financial_capacity(body: CapacityRequest) -> dict[str, Any]:
        from jarvis.bridge.free_cash import capacity_preview

        try:
            return capacity_preview(store, body.fact_ids, start=body.start, end=body.end,
                                    currency=body.currency, scenarios=body.scenarios)
        except (ValueError, sqlite3.Error):
            raise HTTPException(422, "Invalid financial horizon or unavailable review snapshot") from None

    def filtered_queue(filters: QueueFilter) -> dict[str, Any]:
        classification, reason, offset, status = filters.classification, filters.reason, filters.offset, filters.status
        if filters.start and filters.end and filters.start > filters.end:
            raise HTTPException(422, "Начало периода позже окончания")
        from jarvis.finance.review_backup import review_snapshot
        from jarvis.finance.review_labels import HumanLabels
        with review_snapshot(store) as snapshot:
            items = snapshot.items()
            decisions = len(snapshot.audit())
            labels = HumanLabels(snapshot)
            label_decisions = labels.latest()
            progress = {"total": len(items), "statuses": dict(Counter(i["status"] for i in items)),
                        "confidence": dict(Counter(i["classification"] for i in items)),
                        "human_labels": sum(e["action"] == "confirm" for e in label_decisions.values()),
                        "label_policy_status": "configured" if labels.policy() else "required"}
        selected = [i for i in items if (not classification or i["classification"] == classification)
                    and (not status or i["status"] == status)
                    and (not reason or reason in {r for c in i["candidates"] for r in c["reasons"]}
                         or reason == i.get("reason")
                         or reason == "missing lineage") and _matches_queue_filter(i, filters)]
        return {"total": len(selected), "decisions": decisions, "progress": progress, "production_database_modified": False,
                "items": [{"id": i["id"], "card": i.get("statement_card"), "date": i["operation"].get("tx_date"),
                           "source_kind": i.get("source_kind", "pdf"),
                           "source_label": ("Generated PDF: финансовые данные не подтверждены" if i.get("source_kind") == "generated_pdf" else "Sber MCP: " + i["bank_account"].split(":", 1)[0]
                                            if i.get("source_kind") == "sber_mcp"
                                            else i.get("statement_card") or "Счёт из выписки"),
                           "amount": i["operation"].get("amount"), "currency": i["operation"].get("currency"),
                           "description": i["operation"].get("description"), "merchant": i["operation"].get("merchant"),
                           "label_decision": label_decisions.get(i["id"]),
                           "classification": i["classification"], "status": i["status"]}
                          for i in selected[offset:offset + 50]]}

    @app.get("/api/queue", dependencies=[Depends(authorized)])
    def queue(classification: str = "", reason: str = "", offset: int = Query(0, ge=0),
              status: Literal["", "pending_review", "confirm_existing", "approve_new", "reject"] = "") -> dict[str, Any]:
        return filtered_queue(QueueFilter(classification=classification, reason=reason, offset=offset, status=status))

    @app.post("/api/queue-search", dependencies=[Depends(authorized)])
    def queue_search(filters: QueueFilter) -> dict[str, Any]:
        return filtered_queue(filters)

    @app.get("/api/items/{item_id}", dependencies=[Depends(authorized)])
    def item(item_id: str) -> dict[str, Any]:
        from jarvis.finance.classification.suggestions import LocalSuggestions
        from jarvis.finance.review_labels import HumanLabels
        for entry in store.items():
            if entry["id"] == item_id:
                label = HumanLabels(store).latest().get(item_id)
                return {**entry, "label_decision": label, "label_revision": label["seq"] if label else 0,
                        "category_suggestion": LocalSuggestions(store).preview(item_id).model_dump()}
        raise HTTPException(404, "Unknown review item")

    @app.get("/api/insights", dependencies=[Depends(authorized)])
    def insight_cards() -> dict[str, Any]:
        return agent_runtime.insight_cards() if agent_runtime is not None else {"status": "insufficient_data", "cards": [],
            "notices": [], "reason": "No guarded agent journal configured", "production_database_modified": False}

    @app.post("/api/insights/scan", dependencies=[Depends(authorized)])
    async def insight_scan(body: InsightScan) -> dict[str, Any]:
        if agent_runtime is None:
            raise HTTPException(503, "Private agent journal required")
        return await agent_runtime.financial_insights_event(body.month, body.currency)

    @app.post("/api/insights/decision", dependencies=[Depends(authorized)])
    def insight_decision(body: InsightDecision) -> dict[str, Any]:
        if agent_runtime is None:
            raise HTTPException(503, "Private agent journal required")
        return agent_runtime.decide_insight(**body.model_dump())

    @app.get("/api/labels/progress", dependencies=[Depends(authorized)])
    def label_progress() -> dict[str, Any]:
        from jarvis.finance.review_labels import HumanLabels
        return HumanLabels(store).progress()

    @app.get("/api/teacher/status", dependencies=[Depends(authorized)])
    def teacher_status() -> dict[str, Any]:
        from jarvis.core.config import settings
        grants = []
        permissions = TeacherPermissions(store)
        for key, payload in store.metadata().items():
            if key.startswith("teacher_policy:"):
                content = json.loads(payload)
                policy = TeacherPolicy.model_validate(content["policy"])
                ident = key.split(":", 1)[1]
                try:
                    permissions.check(ident, synthetic=policy.mode == "mock")
                    status = "active"
                except PermissionError:
                    status = "unavailable"
                grants.append({"permission_id": ident, "status": status, "policy": policy.model_dump(mode="json")})
        return {"permissions": grants, "mode": settings.llm_mode, "configured": ai_budget_path is not None and ai_pricing_path is not None,
                "transport_verified": False, "financial_apply_allowed": False}

    @app.post("/api/teacher/grant", dependencies=[Depends(authorized)])
    def teacher_grant(body: TeacherGrant) -> dict[str, Any]:
        ident = TeacherPermissions(store).grant(body.policy, confirmed=body.confirmed, actor=body.actor)
        return {"permission_id": ident, "financial_apply_allowed": False}

    @app.post("/api/teacher/revoke", dependencies=[Depends(authorized)])
    def teacher_revoke(body: TeacherRevoke) -> dict[str, Any]:
        TeacherPermissions(store).revoke(body.permission_id, actor=body.actor)
        return {"status": "revoked", "financial_apply_allowed": False}

    @app.post("/api/items/{item_id}/teacher-preview", dependencies=[Depends(authorized)])
    def teacher_preview(item_id: str, body: TeacherAction) -> dict[str, Any]:
        import httpx
        evidence = TeacherPermissions(store).evidence(body.permission_id, item_id, synthetic=isinstance(ai_mock_transport, httpx.MockTransport))
        return {**evidence, "maximum_output_tokens": 400, "request_budget_rub": "0.03", "paid_request_sent": False}

    @app.post("/api/items/{item_id}/teacher", dependencies=[Depends(authorized)])
    async def teacher_propose(item_id: str, body: TeacherProposal) -> dict[str, Any]:
        from jarvis.core.llm.orchestrator import complete_category_candidate
        if ai_budget_path is None or ai_pricing_path is None:
            raise HTTPException(503, "Нужны существующий RUB journal и свежий каталог цен; данные не отправлены")
        return await complete_category_candidate(permissions=TeacherPermissions(store), permission_id=body.permission_id,
            item_id=item_id, budget_path=ai_budget_path, pricing_path=ai_pricing_path, transport=ai_mock_transport,
            expected_binding=body.expected_binding)

    @app.get("/api/agent/tasks", dependencies=[Depends(authorized)])
    def agent_tasks() -> dict[str, Any]:
        if agent_runtime is None:
            return {"status": "unavailable", "reason": "No staging agent journal configured", "tasks": None}
        report = agent_runtime.results()
        bound = {r["task_id"]: r for r in public_research_results()["tasks"]}
        report["tasks"] = [bound.get(row["task_id"], row) for row in report["tasks"]]
        return report

    @app.post("/api/agent/local-analysis", dependencies=[Depends(authorized)])
    async def agent_local_analysis(body: LocalAnalysisRun) -> dict[str, Any]:
        if agent_runtime is None:
            raise HTTPException(503, "Нужен приватный журнал задач: запустите UI с --agent-journal, без daemon")
        return await agent_runtime.local_analysis_event(body.month, body.currency,
            deadline=agent_runtime.clock() + timedelta(minutes=30))

    @app.get("/api/agent/qwen-readiness", dependencies=[Depends(authorized)])
    def agent_qwen_readiness(month: str | None = None) -> dict[str, Any]:
        import httpx

        from jarvis.finance.financial_analyst import analyst_preflight
        return analyst_preflight(store, month, synthetic=isinstance(
            agent_runtime.assistant.transport if agent_runtime else ai_mock_transport, httpx.MockTransport))

    @app.post("/api/agent/qwen-analysis", dependencies=[Depends(authorized)])
    async def agent_qwen_analysis(body: QwenPilotRun) -> dict[str, Any]:
        import httpx

        from jarvis.finance.financial_analyst import (
            AnalystPermissions,
            analyst_preflight,
            selected_analyst,
        )
        if agent_runtime is None:
            raise HTTPException(503, "Нужен приватный journal; платный запрос не отправлен")
        synthetic = isinstance(agent_runtime.assistant.transport, httpx.MockTransport)
        try:
            selected = selected_analyst(store)
            AnalystPermissions(store).check(selected['permission_id'], synthetic=synthetic)
        except (PermissionError, ValueError, OSError, sqlite3.Error):
            raise HTTPException(403, "QWEN_SETUP_REQUIRED: действующее разрешение отсутствует") from None
        preview = analyst_preflight(store, body.month, synthetic=synthetic)
        if not preview['local_eligible']:
            return {"pilot_status": "UNKNOWN" if preview["status"] == "UNKNOWN" else "BLOCKED", "reason": preview['reason'], "preflight": preview,
                "production_database_modified": False}
        if (not synthetic and not body.owner_confirmed or
                (body.preview_binding != preview.get('preview_binding') and (not synthetic or body.preview_binding is not None))):
            raise HTTPException(409, "Нужно отдельное owner-подтверждение актуального preview; HTTP не отправлен")
        result = await agent_runtime.financial_analysis_event(body.month, deadline=agent_runtime.clock() + timedelta(minutes=5),
            expected_permission_id=preview['permission_id'], expected_binding=preview['source_scope']['binding'])
        observation = result.get('data', {}).get('observation', {})
        pilot_status = ('SUCCESS' if result.get('state') == 'COMPLETED' else
            'UNKNOWN' if result.get('state') == 'UNKNOWN' or observation.get('status') == 'uncertain' else 'REFUSED')
        return {**result, 'pilot_status': pilot_status, 'genuine_model_result': result.get('state') == 'COMPLETED' and not synthetic}

    @app.post("/api/agent/financial-analysis", dependencies=[Depends(authorized)])
    async def agent_financial_analysis(body: FinancialAnalysisRun) -> dict[str, Any]:
        import httpx
        if (agent_runtime is None or not isinstance(agent_runtime.assistant.transport, httpx.MockTransport)
                or agent_runtime.public_sources is None or not agent_runtime.public_sources.synthetic):
            raise HTTPException(503, "Интегрированный анализ пока только offline/mock; live-разрешение не выдано")
        return await agent_runtime.financial_analysis_event(body.month, body.public_task_id,
            deadline=agent_runtime.clock() + timedelta(minutes=5))

    @app.post("/api/agent/tasks/{task_id}/cancel", dependencies=[Depends(authorized)])
    def agent_cancel(task_id: int) -> dict[str, Any]:
        if agent_runtime is None:
            raise HTTPException(503, "Agent journal unavailable")
        return agent_runtime.cancel(task_id)

    @app.get("/api/items/{item_id}/group", dependencies=[Depends(authorized)])
    def group(item_id: str) -> dict[str, Any]:
        from jarvis.finance.review_batch import group_preview
        return group_preview(store, item_id)

    @app.get("/api/items/{item_id}/category-suggestion", dependencies=[Depends(authorized)])
    def category_suggestion(item_id: str) -> dict[str, Any]:
        from jarvis.finance.classification.suggestions import LocalSuggestions
        if not any(row["id"] == item_id for row in store.items()):
            raise HTTPException(404, "Unknown review item")
        return LocalSuggestions(store).preview(item_id).model_dump()

    @app.post("/api/items/{item_id}/group", dependencies=[Depends(authorized)])
    def group_decide(item_id: str, decision: GroupDecision) -> dict[str, Any]:
        from jarvis.finance.review_batch import decide_group
        return decide_group(store, item_id, **decision.model_dump())

    @app.post("/api/items/{item_id}/decision", dependencies=[Depends(authorized)])
    def decide(item_id: str, decision: Decision) -> dict[str, Any]:
        return store.decide(item_id, **decision.model_dump())

    @app.get("/api/audit", dependencies=[Depends(authorized)])
    def audit() -> dict[str, Any]:
        from jarvis.finance.review_labels import HumanLabels
        return {"packet_hash": store.metadata()["packet_hash"], "events": store.audit(), "label_events": HumanLabels(store).audit()}

    @app.get("/api/staging-plan", dependencies=[Depends(authorized)])
    def staging_plan() -> dict[str, Any]:
        from jarvis.finance.review_staging import build_staging_plan
        return build_staging_plan(store)

    @app.get("/api/export", dependencies=[Depends(authorized)])
    def review_export() -> Any:
        from fastapi.responses import JSONResponse

        from jarvis.finance.review_export import build_review_export
        return JSONResponse(build_review_export(store), headers={"Cache-Control": "no-store",
                            "Content-Disposition": 'attachment; filename="private-review-packet.json"'})

    @app.get("/api/report", dependencies=[Depends(authorized)])
    def product_report() -> dict[str, Any]:
        from jarvis.finance.review_sources import workspace_report
        return workspace_report(store)

    @app.get("/api/workbench", dependencies=[Depends(authorized)])
    def workbench() -> dict[str, Any]:
        from jarvis.finance.review_workbench import workbench_overview
        return workbench_overview(store)

    @app.post("/api/financial-question", dependencies=[Depends(authorized)])
    def financial_query(body: FinancialQuestion) -> dict[str, Any]:
        from jarvis.finance.audit_report import financial_question
        ai_observations["local_questions"] += 1
        if body.statement_scope is not None:
            return financial_question(None, intent=body.intent, currency=body.currency,
                statement_scope=body.statement_scope, review_store=store, question=body.question)
        if ai_budget_path is not None:
            from jarvis.core.config import settings
            from jarvis.core.llm.budget_store import BudgetStore, BudgetStoreUnavailable
            try:
                BudgetStore(ai_budget_path, currency="RUB", simulation=settings.llm_mode == "mock").observe("local_only")
            except BudgetStoreUnavailable:
                ai_observations["source_error"] = "telemetry_unavailable"
        if ledger_path is None:
            return {"status": "insufficient_data", "answer": None, "intent": body.intent,
                    "reason": "No accepted scoped ledger configured", "database_modified": False, "apply_allowed": False}
        return financial_question(ledger_path, month=body.month, intent=body.intent,
            currency=body.currency, accepted_sources=accepted_sources, accepted_sha256=accepted_sha256,
            question=body.question)

    @app.get("/api/ai/status", dependencies=[Depends(authorized)])
    def ai_status() -> dict[str, Any]:
        if web_only:
            return {'status':'disabled_web_only', 'dispatch_allowed':False, 'execution_allowed':False}
        from jarvis.core.config import settings
        from jarvis.core.llm.budget_store import BudgetStore, BudgetStoreUnavailable
        result = {"status": "unavailable", "mode": settings.llm_mode, "model": settings.aitunnel_model,
                  "database_modified": False, "simulation": settings.llm_mode == "mock", **ai_observations}
        if ai_budget_path is not None:
            try:
                summary = BudgetStore(ai_budget_path, currency="RUB", simulation=settings.llm_mode == "mock").summary(now=datetime.now(UTC))
                result.update(summary, status="available", remaining_daily=str(Decimal(str(settings.daily_budget_rub)) - Decimal(str(summary["committed_today"]))),
                    remaining_monthly=str(Decimal(str(settings.monthly_budget_rub)) - Decimal(str(summary["committed_month"]))))
                result.update(daily_limit_rub=str(settings.daily_budget_rub), monthly_limit_rub=str(settings.monthly_budget_rub))
            except BudgetStoreUnavailable:
                result["source_error"] = "budget_journal_unavailable"
        return result

    @app.post("/api/ai/owner-key-check", dependencies=[Depends(authorized)])
    async def ai_owner_key_check() -> dict[str, object]:
        from jarvis.core.llm.errors import LLMUnavailable
        from jarvis.core.llm.providers.openai_compat import OpenAICompatProvider
        try:
            return await OpenAICompatProvider(base_url="https://api.aitunnel.ru/v1").owner_key_status(owner_action=True)
        except LLMUnavailable:
            return {"status": "unavailable", "reason": "Separate owner process/key policy check required", "live_readiness": False}

    @app.post("/api/ai/activate", dependencies=[Depends(authorized)])
    async def ai_activate(body: OwnerAIActivation) -> dict[str, Any]:
        import os

        import httpx

        from jarvis.core.config import settings
        from jarvis.core.llm.budget_store import (
            BudgetReservationDenied,
            BudgetStore,
            BudgetStoreUnavailable,
        )
        from jarvis.core.llm.errors import LLMError, LLMUnavailable
        from jarvis.core.llm.orchestrator import complete_category_candidate
        from jarvis.core.llm.pricing import aitunnel_price_evidence
        from jarvis.core.llm.providers.openai_compat import OpenAICompatProvider

        synthetic = isinstance(ai_mock_transport, httpx.MockTransport)
        if not body.confirmed or settings.aitunnel_model != "qwen3.7-flash":
            raise HTTPException(403, "Нужны явное согласие и модель qwen3.7-flash")
        if synthetic != (body.policy.mode == "mock") or (synthetic and settings.llm_mode != "mock"):
            raise HTTPException(403, "Тестовое разрешение не подходит для реального транспорта")
        if not synthetic and (os.environ.get("CODEX_THREAD_ID") or os.environ.get("PYTEST_CURRENT_TEST")):
            raise HTTPException(403, "Откройте отдельный локальный процесс владельца; Codex не активирует cloud")
        if ai_budget_path is None or not ai_budget_path.is_file() or ai_pricing_path is None:
            raise HTTPException(503, "Выберите существующий RUB journal и свежий каталог; новый журнал не создаётся")
        previous_mode = settings.llm_mode
        permissions = TeacherPermissions(store)
        ident = None
        try:
            price = aitunnel_price_evidence(ai_pricing_path, "qwen3.7-flash", now=datetime.now(UTC))
            daily = min(Decimal(str(settings.daily_budget_rub)), body.policy.daily_budget_rub, Decimal(2))
            monthly = min(Decimal(str(settings.monthly_budget_rub)), body.policy.monthly_budget_rub, Decimal(50))
            if price.reserved_rub > min(Decimal("0.03"), body.policy.request_budget_rub, Decimal(str(settings.request_ceiling_rub))):
                raise ValueError("Request reserve exceeds scope")
            capacity = BudgetStore(ai_budget_path, currency="RUB", simulation=synthetic).capacity(
                price.reserved_rub, daily, monthly, now=datetime.now(UTC))
            if not capacity["can_reserve"]:
                raise ValueError("No local budget capacity")
            if not synthetic:
                settings.llm_mode = "cloud_opt_in"
            key_status = await OpenAICompatProvider(api_key="synthetic-activation" if synthetic else None,
                base_url="https://api.aitunnel.ru/v1", transport=ai_mock_transport).owner_key_status(owner_action=True)
            if key_status["status"] != "ready" or (not synthetic and not key_status["live_readiness"]):
                raise LLMUnavailable("Owner key restrictions refused")
            ident = permissions.grant(body.policy, confirmed=True, actor=body.actor, request_id=body.action_id)
        except (LLMError, BudgetStoreUnavailable, BudgetReservationDenied, ValueError, PermissionError, OSError, sqlite3.Error):
            raise HTTPException(503, "Активация отклонена: проверьте journal, цены, срок и ограничения ключа; запрос Qwen не отправлен") from None
        finally:
            if ident is None:
                settings.llm_mode = previous_mode
        # The owner action grants only the selected purpose, never a financial apply.
        result: dict[str, Any] = {"status": "activated", "permission_id": ident, "key_status": key_status,
            "mode": settings.llm_mode, "effective_budget": capacity, "financial_apply_allowed": False}
        if body.policy.purpose == "public_development":
            try:
                result["first_response"] = await complete_category_candidate(permissions=permissions,
                    permission_id=ident, item_id="public-smoke", budget_path=ai_budget_path,
                    pricing_path=ai_pricing_path, transport=ai_mock_transport)
            except (LLMError, BudgetStoreUnavailable, BudgetReservationDenied, ValueError, PermissionError, OSError, sqlite3.Error):
                # Do not retry or invent a receipt after any possible external effect.
                result["first_response"] = {"status": "unavailable", "reason": "Read existing RUB journal; no automatic resend"}
        return result

    @app.post("/api/ai/disable", dependencies=[Depends(authorized)])
    def ai_disable() -> dict[str, Any]:
        from jarvis.core.config import settings
        settings.llm_mode = "offline"
        return {"mode": "offline", "scope": "current_process_future_requests", "database_modified": False}

    @app.post("/api/knowledge-question", dependencies=[Depends(authorized)])
    async def knowledge_question(body: PublicQuestion) -> dict[str, Any]:
        if knowledge_retriever is None or ai_budget_path is None or ai_pricing_path is None:
            return {"status": "unavailable", "reason": "Approved public knowledge route unconfigured", "answer": None}
        result = await knowledge_retriever.explain(body.question, approved_sources=knowledge_sources,
            budget_path=ai_budget_path, pricing_path=ai_pricing_path)
        ai_observations["source_error"] = result.get("llm", {}).get("reason")
        return result

    @app.get("/api/memory", dependencies=[Depends(authorized)])
    def memory() -> dict[str, Any]:
        from jarvis.finance.review_labels import HumanLabels
        return HumanLabels(store).memory()

    @app.post("/api/memory-notes", dependencies=[Depends(authorized)])
    def memory_note(body: MemoryNote) -> dict[str, Any]:
        ident = store.record_memory_note(body.kind, body.text, actor=body.actor,
            model_version=body.model_version, expires_at=body.expires_at)
        return {"id": ident, "review_database_modified": True, "production_database_modified": False,
                "financial_apply_allowed": False}

    @app.post("/api/memory-notes/{ident}/revoke", dependencies=[Depends(authorized)])
    def revoke_note(ident: str, body: MemoryRevocation) -> dict[str, Any]:
        store.revoke_memory_note(ident, actor=body.actor, expected_revision=body.expected_revision)
        return {"review_database_modified": True, "production_database_modified": False, "financial_apply_allowed": False}

    @app.get("/api/availability", dependencies=[Depends(authorized)])
    def availability() -> Any:
        from fastapi.responses import JSONResponse

        from jarvis.finance.review_availability import source_availability
        return JSONResponse(source_availability(store), headers={"Cache-Control": "no-store"})

    @app.get("/api/sber-connection", dependencies=[Depends(authorized)])
    def sber_connection() -> dict[str, Any]:
        from jarvis.finance.sber_operator import connection_status
        return connection_status()

    @app.get("/api/provenance", dependencies=[Depends(authorized)])
    def provenance() -> Any:
        from fastapi.responses import JSONResponse

        from jarvis.finance.review_provenance import provenance_matrix
        return JSONResponse(provenance_matrix(store), headers={"Cache-Control": "no-store"})

    @app.get("/api/sources/{sha}", dependencies=[Depends(authorized)])
    def pdf(sha: str, expected_revision: int | None = Query(None, ge=0, le=1000000000)) -> Any:
        from fastapi.responses import FileResponse, JSONResponse

        from jarvis.finance.review_sources import source_pdf
        packet = json.loads(store.metadata()["packet"])
        if packet.get("source_kind") == "generated_pdf":
            import base64
            from datetime import UTC, datetime

            from jarvis.finance.review_pdf_evidence import generated_source_state
            with store.connect() as lock:
                lock.execute("BEGIN IMMEDIATE")
                packet = json.loads(store.metadata()["packet"])
                state = generated_source_state(store, now=datetime.now(UTC), expected_revision=expected_revision)
                if state["status"] != "available":
                    raise HTTPException(409, {"status": state["status"], "financial_source_approved": False})
                source = next((s for s in packet["pdf_sources"] if s["sha256"] == sha), None)
                if source is None:
                    raise HTTPException(404, "Verified source unavailable")
                return Response(base64.b64decode(source["original_base64"], validate=True), media_type="application/pdf",
                    headers={"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="private-generated.pdf"',
                             "X-Review-Revision": str(state["review_revision"])})
        if json.loads(store.metadata()["packet"]).get("source_kind") == "sber_mcp":
            from jarvis.finance.sber_intake import native_source
            return JSONResponse(native_source(store, sha), headers={"Cache-Control": "no-store"})
        return FileResponse(source_pdf(store, sha), media_type="application/pdf", headers={"Cache-Control": "no-store"})

    @app.get("/api/labels/policy", dependencies=[Depends(authorized)])
    def label_policy() -> dict[str, Any]:
        from jarvis.finance.review_labels import HumanLabels
        return HumanLabels(store).policy() or {"status": "policy_required", "categories": []}

    @app.get("/api/labels/export", dependencies=[Depends(authorized)])
    def labels_export() -> dict[str, Any]:
        from jarvis.finance.review_labels import HumanLabels
        return HumanLabels(store).export()

    @app.post("/api/items/{item_id}/label", dependencies=[Depends(authorized)])
    def label_decide(item_id: str, decision: LabelDecision) -> dict[str, Any]:
        from jarvis.finance.review_labels import HumanLabels
        return HumanLabels(store).decide(item_id, **decision.model_dump())

    return app
