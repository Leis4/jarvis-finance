"""Process-bound owner sessions; bootstrap credentials never enter page source."""
from __future__ import annotations

import csv
import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import HTTPException, Request
from starlette.responses import Response

COOKIE = 'jarvis_owner_session'
DEVICE_COOKIE = 'jarvis_owner_device'
DEVICE_TTL_DAYS = 90
SESSION_SECONDS = 8 * 60 * 60
READ_ONLY_POSTS = frozenset({'/api/queue-search', '/api/cfo/history', '/api/cfo/search',
    '/api/cfo/question', '/api/cfo/tools'})
# Owner-authored local edits: ledger rows the owner types, category confirmations,
# budgets, goals, profile, schedule settings and the local AI chat store. They never
# touch the bank session, statement imports or Telegram, so they stay usable in
# web-only mode. Paid model calls remain governed by the shared RUB budget journal.
OWNER_EDIT_POSTS = frozenset({'/api/cfo/expenses', '/api/cfo/goals',
    '/api/cfo/purchase', '/api/cfo/backup', '/api/cfo/group-review',
    '/api/ai/sessions', '/api/ai/memory',
    '/api/auth/pair/start', '/api/auth/logout-all',
    '/api/auth/passkey/register/begin', '/api/auth/passkey/register/complete',
    '/api/ai/autopilot/classify', '/api/cfo/budget-plan/apply'})
OWNER_EDIT_PUTS = frozenset({'/api/cfo/category-budget', '/api/cfo/profile',
    '/api/cfo/plan', '/api/cfo/runtime', '/api/ai/autopilot'})
OWNER_EDIT_PATTERNS = (
    ('POST', r'/api/cfo/category/[0-9]+/undo'),
    ('PUT', r'/api/cfo/category/[0-9]+'),
    ('POST', r'/api/ai/sessions/[0-9a-f]{32}/message'),
    ('POST', r'/api/ai/memory/[0-9a-f]{32}/decide'),
    ('POST', r'/api/qwen/journal/[a-f0-9]{16,64}/settle'),
    ('PATCH', r'/api/ai/sessions/[0-9a-f]{32}'),
    ('DELETE', r'/api/ai/sessions/[0-9a-f]{32}'),
    ('DELETE', r'/api/ai/memory/[0-9a-f]{32}'),
    ('POST', r'/api/auth/pair/[0-9a-f]{32}/approve'),
    ('POST', r'/api/auth/devices/[0-9a-f]{32}/revoke'),
)
# Read-only bank capture stays available from the web: it only reads accounts
# and history, never payments. PIN delivery happens through the local SDK inbox.
BANK_READ_POSTS = frozenset({'/api/cfo/refresh', '/api/cfo/submit-pin'})
READ_ONLY_GETS = frozenset({
    '/api/cfo', '/api/cfo/health', '/api/cfo/ai-budget', '/api/cfo/briefing',
    '/api/cfo/overview', '/api/cfo/learning', '/api/cfo/clusters', '/api/cfo/proposal-groups',
    '/api/cfo/analytics', '/api/cfo/runtime', '/api/cfo/data-status',
    '/api/cfo/goals', '/api/cfo/categories',
    '/api/ai/sessions', '/api/ai/memory', '/api/ai/search', '/api/qwen/status',
    '/api/qwen/journal', '/api/auth/devices',
    '/api/ai/autopilot', '/api/cfo/budget-plan',
    '/api/groups', '/api/queue', '/api/insights', '/api/labels/progress', '/api/teacher/status',
    '/api/agent/tasks', '/api/audit', '/api/staging-plan', '/api/export', '/api/report',
    '/api/workbench', '/api/ai/status', '/api/memory', '/api/availability',
    '/api/sber-connection', '/api/provenance', '/api/labels/policy', '/api/labels/export',
    '/api/research/status', '/api/research/tasks', '/api/telegram/status',
    '/api/statements', '/api/statements/annotation-export', '/api/statements/annotation-context',
    '/api/statements/annotation-proposals', '/api/statements/history',
    '/api/statements/category-review', '/api/statements/pending-review', '/api/statements/group-preview',
})
READ_ONLY_GET_PATTERNS = (
    r'/api/financial-facts/[^/]+', r'/api/items/[^/]+', r'/api/items/[^/]+/group',
    r'/api/items/[^/]+/category-suggestion', r'/api/cfo/similar/[^/]+', r'/api/sources/[^/]+',
    r'/api/statements/[^/]+/original', r'/api/statements/[^/]+/provenance',
    r'/api/ai/sessions/[0-9a-f]{32}/messages',
    r'/api/auth/pair/[0-9a-f]{32}',
)


def _public_origin() -> str:
    """Configured HTTPS front door (Cloudflare Tunnel + Access), if any."""
    from jarvis.core.config import settings

    value = (settings.public_origin or "").strip().rstrip("/")
    return value if value.startswith("https://") and "/" not in value[8:] else ""


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def allowed_hosts() -> frozenset[str]:
    """Loopback hosts always; the configured public hostname only when set."""
    hosts = set(LOOPBACK_HOSTS)
    origin = _public_origin()
    if origin:
        hosts.add(origin[8:])
    return frozenset(hosts)


def origin_candidates(host: str) -> frozenset[str]:
    """Origins this app accepts for owner writes on the given Host header."""
    candidates: set[str] = set()
    if host.split(":")[0] in LOOPBACK_HOSTS:
        candidates.add("http://" + host)
    shared = _public_origin()
    if shared:
        # TLS terminates at the Cloudflare edge for the configured front door.
        candidates.update({shared, "https://" + host})
    return frozenset(candidates)


def web_only_allowed(method: str, path: str) -> bool:
    """Deny writes by default, including newly registered plugin routes."""
    if method in {'GET', 'HEAD', 'OPTIONS'}:
        # Chat history purges retained messages; Qwen readiness can inspect a provider.
        return path in READ_ONLY_GETS or any(re.fullmatch(pattern, path) for pattern in READ_ONLY_GET_PATTERNS)
    if method == 'POST':
        return path in READ_ONLY_POSTS or path in BANK_READ_POSTS or owner_edit_allowed(method, path)
    return owner_edit_allowed(method, path)


def owner_edit_allowed(method: str, path: str) -> bool:
    """True for the narrow local edit set, used by the UI to label capabilities."""
    if method == 'POST' and path in OWNER_EDIT_POSTS:
        return True
    if method == 'PUT' and path in OWNER_EDIT_PUTS:
        return True
    return any(method == allowed and re.fullmatch(pattern, path)
        for allowed, pattern in OWNER_EDIT_PATTERNS)


@dataclass
class OwnerSession:
    csrf: str
    expires: float


class OwnerAuth:
    def __init__(self, key: str, store: Any = None) -> None:
        self.key = key
        self.sessions: dict[str, OwnerSession] = {}
        self.store = store

    def matches(self, value: str) -> bool:
        try:
            supplied = value.encode('utf-8')
        except UnicodeError:
            return False
        return secrets.compare_digest(supplied, self.key.encode('utf-8'))

    def session(self, request: Request) -> OwnerSession | None:
        ident = request.cookies.get(COOKIE, '')
        value = self.sessions.get(ident)
        if value is None and ident and self.store is not None:
            # After a backend restart the in-memory map is empty; the private
            # SQLite row restores a still-valid session created before it.
            row = self.store.load_session(ident)
            if row is not None:
                remaining = (datetime.fromisoformat(row['expires_at'])
                             - datetime.now(UTC)).total_seconds()
                if remaining > 0:
                    value = OwnerSession(row['csrf'], monotonic() + remaining)
                    self.sessions[ident] = value
        if value is not None and monotonic() >= value.expires:
            self.sessions.pop(ident, None)
            return None
        return value

    @staticmethod
    def same_origin(request: Request) -> None:
        """Owner writes must come from this app, never from another site."""
        host = request.headers.get('host', '')
        origin = request.headers.get('origin')
        if not origin or origin not in origin_candidates(host):
            raise HTTPException(403, 'Same-origin owner action required')

    def authorize(self, request: Request) -> None:
        supplied = request.headers.get('x-review-token')
        if supplied is not None:
            # Explicit API credentials remain compatible; they are never rendered.
            if not self.matches(supplied):
                raise HTTPException(403, 'Owner authentication required')
            return
        session = self.session(request)
        if session is None:
            raise HTTPException(403, 'Owner authentication required')
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            self.same_origin(request)
            csrf = request.headers.get('x-owner-csrf', '')
            if not secrets.compare_digest(csrf.encode('utf-8'), session.csrf.encode('utf-8')):
                raise HTTPException(403, 'Owner CSRF check failed')

    def sign_in(self, response: Response, *, secure: bool = False,
                device_id: str | None = None) -> str:
        for ident in [ident for ident, value in self.sessions.items() if value.expires <= monotonic()]:
            self.sessions.pop(ident, None)
        if len(self.sessions) >= 16:
            self.sessions.pop(next(iter(self.sessions)))
        ident = secrets.token_urlsafe(32)
        session = OwnerSession(secrets.token_urlsafe(32), monotonic() + SESSION_SECONDS)
        self.sessions[ident] = session
        if self.store is not None:
            try:
                self.store.save_session(ident, session.csrf,
                    datetime.now(UTC) + timedelta(seconds=SESSION_SECONDS), device_id)
            except (OSError, ValueError):
                pass
        # HTTP is intentionally limited to loopback; no Domain makes this host-only.
        response.set_cookie(COOKIE, ident, max_age=SESSION_SECONDS, httponly=True,
            samesite='strict', path='/', secure=secure)
        return ident

    def sign_out(self, request: Request, response: Response) -> None:
        ident = request.cookies.get(COOKIE, '')
        self.sessions.pop(ident, None)
        if self.store is not None and ident:
            try:
                self.store.delete_session(ident)
            except (OSError, ValueError):
                pass
        response.delete_cookie(COOKIE, httponly=True, samesite='strict', path='/')


def publish_owner_key(workspace: Path, key: str) -> Path:
    """Replace the owner-only key file atomically, without logging its contents."""
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.owner-key-', dir=workspace)
    os.close(descriptor)
    temporary = Path(name)
    target = workspace / 'owner-login.key'
    try:
        if os.name == 'nt':
            system = Path(os.environ['SystemRoot']) / 'System32'
            whoami = str(system / 'whoami.exe')
            icacls = str(system / 'icacls.exe')
            identity = subprocess.run([whoami, '/user', '/fo', 'csv', '/nh'], executable=whoami, check=True,
                capture_output=True, text=True, timeout=10)
            sid = next(csv.reader(identity.stdout.strip().splitlines()))[-1]
            if not sid.startswith('S-1-'):
                raise ValueError('Owner SID unavailable')
            subprocess.run([icacls, str(temporary), '/inheritance:r', '/grant:r',
                f'*{sid}:(F)', '*S-1-5-18:(F)'], executable=icacls, check=True, capture_output=True, timeout=10)
        else:
            temporary.chmod(0o600)
        temporary.write_text(key, encoding='utf-8')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
