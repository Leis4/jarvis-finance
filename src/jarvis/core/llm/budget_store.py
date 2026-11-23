"""Persistent SQLite reservation ledger for bounded LLM spending."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4


class BudgetStoreUnavailable(RuntimeError):
    """Budget ledger cannot be read or written safely."""


class BudgetReservationDenied(RuntimeError):
    """Request exceeds a limit or violates reservation state."""


@dataclass(frozen=True)
class UsageReceipt:
    """Private-content-free evidence associated with a settled reservation."""

    model: str
    tokens_in: int
    tokens_out: int
    prompt_sha256: str
    output_sha256: str

    def values(self) -> tuple[str, int, int, str, str]:
        if (not self.model or type(self.tokens_in) is not int or self.tokens_in <= 0 or
                type(self.tokens_out) is not int or self.tokens_out < 0 or
                any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                    for value in (self.prompt_sha256, self.output_sha256))):
            raise ValueError("invalid usage receipt")
        return self.model, self.tokens_in, self.tokens_out, self.prompt_sha256, self.output_sha256


GLOBAL_MONTHLY_LIMIT_RUB = Decimal(500)
PURPOSE_LIMITS_RUB = {
    'dialogue': Decimal(300), 'classification': Decimal(80), 'research': Decimal(40),
    'maintenance': Decimal(30), 'reserve': Decimal(50),
}


_OPEN = frozenset({"reserved", "attempted", "uncertain"})


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("budget timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _money(value: Decimal | float | str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid budget amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid budget amount")
    return amount


class BudgetStore:
    """Atomic budget reservations within the existing LLM SQLite database."""

    def __init__(self, path: Path, *, currency: str = "USD", simulation: bool = False) -> None:
        if currency not in {"USD", "RUB"}:
            raise ValueError("Unsupported budget currency")
        self.path = path
        self.currency = currency
        prefix = "llm_mock_rub_budget" if simulation else "llm_rub_budget"
        self.reservations = "llm_budget_reservations" if currency == "USD" else prefix + "_reservations"
        self.receipts = "llm_budget_receipts" if currency == "USD" else prefix + "_receipts"
        self.reserved = "reserved_usd" if currency == "USD" else "reserved_rub"
        self.settled = "settled_usd" if currency == "USD" else "settled_rub"
        self.observations = self.reservations.replace("_reservations", "_observations")
        self.usage = self.reservations.replace("_reservations", "_usage")
        self.purpose_limits = self.reservations.replace("_reservations", "_purpose_limits")
        self.purposes = self.reservations.replace("_reservations", "_purposes")

    def enable_global(self) -> None:
        """Extend the existing dedicated CFO RUB journal after an external backup."""
        if self.currency != "RUB":
            raise ValueError("Global policy requires RUB")
        with self._transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS cfo_ai_policy(version INTEGER PRIMARY KEY CHECK(version=1))")
            conn.execute("INSERT OR IGNORE INTO cfo_ai_policy VALUES(1)")
            conn.execute("""CREATE TABLE IF NOT EXISTS cfo_ai_tasks(
                request_id TEXT PRIMARY KEY, task TEXT NOT NULL, model TEXT NOT NULL,
                provider_request_id TEXT, latency_ms INTEGER, finish_reason TEXT, quality TEXT)""")
            conn.execute("CREATE TABLE IF NOT EXISTS cfo_ai_cache(cache_key TEXT PRIMARY KEY, response TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS cfo_ai_provider(id INTEGER PRIMARY KEY CHECK(id=1), initial TEXT, remaining TEXT, observed_at TEXT, account_balance TEXT)")
            if 'account_balance' not in {row[1] for row in conn.execute('PRAGMA table_info(cfo_ai_provider)')}:
                conn.execute('ALTER TABLE cfo_ai_provider ADD COLUMN account_balance TEXT')

    def provider_snapshot(self, status: dict[str, object]) -> None:
        """Record quota evidence without attributing other key consumers to Jarvis tasks."""
        initial, remaining = _money(str(status['budget_initial_rub'])), _money(str(status['budget_remaining_rub']))
        if remaining > initial or status.get('reset_interval') != 'monthly':
            raise ValueError('Provider monthly quota evidence required')
        account = _money(str(status['account_balance_rub'])) if status.get('account_balance_rub') is not None else None
        observed = _utc(datetime.fromisoformat(str(status['checked_at']))).isoformat()
        with self._transaction() as conn:
            conn.execute('INSERT INTO cfo_ai_provider(id,initial,remaining,observed_at,account_balance) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET initial=excluded.initial,remaining=excluded.remaining,observed_at=excluded.observed_at,account_balance=excluded.account_balance WHERE excluded.observed_at>=cfo_ai_provider.observed_at',
                (str(initial), str(remaining), observed, str(account) if account is not None else None))

    def enable_purpose_limits(self) -> None:
        """Opt in to fixed purpose quotas within this existing global RUB journal."""
        if self.currency != 'RUB':
            raise ValueError('Purpose quotas require RUB')
        with self._transaction() as conn:
            global_policy = conn.execute("SELECT 1 FROM sqlite_master WHERE name='cfo_ai_policy'").fetchone()
            if not global_policy or conn.execute('SELECT version FROM cfo_ai_policy').fetchall() != [(1,)]:
                raise ValueError('Global budget policy required before purpose quotas')
            conn.execute(f'CREATE TABLE IF NOT EXISTS {self.purpose_limits}(purpose TEXT PRIMARY KEY, monthly_limit_rub TEXT NOT NULL)')
            conn.execute(f'''CREATE TABLE IF NOT EXISTS {self.purposes}(
                request_id TEXT PRIMARY KEY REFERENCES {self.reservations}(request_id), purpose TEXT NOT NULL)''')
            conn.executemany(f'INSERT OR IGNORE INTO {self.purpose_limits} VALUES(?,?)',
                [(purpose, str(limit)) for purpose, limit in PURPOSE_LIMITS_RUB.items()])
            self._purpose_budgets(conn, datetime.now(UTC))

    def _purpose_budgets(self, conn: sqlite3.Connection, current: datetime) -> dict[str, dict[str, str]] | None:
        enabled = conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (self.purpose_limits,)).fetchone()
        if not enabled:
            return None
        try:
            limits = {purpose: _money(limit) for purpose, limit in conn.execute(f'SELECT * FROM {self.purpose_limits}')}
        except ValueError:
            raise BudgetStoreUnavailable('Invalid purpose budget policy') from None
        if limits != PURPOSE_LIMITS_RUB:
            raise BudgetStoreUnavailable('Invalid purpose budget policy')
        committed = {purpose: Decimal(0) for purpose in limits}
        rows = conn.execute(f'''SELECT COALESCE(p.purpose,'reserve'),r.month_utc,r.status,r.{self.reserved},r.{self.settled}
            FROM {self.reservations} r LEFT JOIN {self.purposes} p USING(request_id)''').fetchall()
        for purpose, month, status, reserved, settled in rows:
            if purpose not in committed:
                raise BudgetStoreUnavailable('Invalid reservation purpose')
            if status in _OPEN:
                committed[purpose] += _money(reserved)
            elif status == 'settled' and month == current.strftime('%Y-%m'):
                if settled is None:
                    raise BudgetStoreUnavailable('Invalid settled budget record')
                committed[purpose] += _money(settled)
        return {purpose: {'limit_rub': str(limit), 'committed_rub': str(committed[purpose]),
                         'remaining_rub': str(max(Decimal(0), limit-committed[purpose]))}
                for purpose, limit in limits.items()}

    def _provider_available(self, conn: sqlite3.Connection) -> Decimal | None:
        row = conn.execute('SELECT remaining,observed_at,account_balance FROM cfo_ai_provider WHERE id=1').fetchone()
        if not row:
            return None
        debits = conn.execute(f"SELECT {self.settled} FROM {self.reservations} WHERE status='settled' AND completed_at>=?", (row[1],)).fetchall()
        available = min(_money(row[0]), _money(row[2])) if row[2] is not None else _money(row[0])
        return max(Decimal(0), available-sum((_money(r[0]) for r in debits), Decimal(0)))

    def task_metadata(self, request_id: str, task: str, model: str, *, request: str | None = None,
                      latency: int | None = None, finish: str | None = None, quality: str | None = None) -> None:
        """Persist content-free dispatch, provider correlation and quality separately from billing."""
        if not re.fullmatch(r"[a-z_]{1,40}", task):
            raise ValueError("Closed task name required")
        if request is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request):
            request = None
        with self._transaction() as conn:
            conn.execute("""INSERT INTO cfo_ai_tasks VALUES(?,?,?,?,?,?,?) ON CONFLICT(request_id)
                DO UPDATE SET provider_request_id=excluded.provider_request_id,latency_ms=excluded.latency_ms,
                finish_reason=excluded.finish_reason,quality=excluded.quality""",
                (request_id, task, model, request, latency, finish, quality))

    def cached(self, key: str) -> dict[str, Any] | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT response FROM cfo_ai_cache WHERE cache_key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def cache_result(self, key: str, response: dict[str, Any]) -> None:
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO cfo_ai_cache VALUES(?,?)", (key, json.dumps(response, ensure_ascii=False)))

    def global_report(self, *, now: datetime) -> dict[str, Any]:
        """Report UTC calendar spend, reserved exposure, model/task cost and soft daily guide."""
        with self._transaction() as conn:
            summary = self.summary(now=now)
            grouped = conn.execute(f"""SELECT COALESCE(t.task,'legacy'),COALESCE(t.model,c.model,'unknown'),
                r.status,r.{self.reserved},r.{self.settled} FROM {self.reservations} r
                LEFT JOIN cfo_ai_tasks t USING(request_id) LEFT JOIN {self.receipts} c USING(request_id)
                WHERE r.month_utc=? OR r.status IN ('reserved','attempted','uncertain')""", (_utc(now).strftime('%Y-%m'),)).fetchall()
            provider_available = self._provider_available(conn)
            quotas = self._purpose_budgets(conn, _utc(now))
            dispatch_allowed = any(self._capacity(conn, Decimal('.01'), GLOBAL_MONTHLY_LIMIT_RUB,
                GLOBAL_MONTHLY_LIMIT_RUB, _utc(now), purpose=purpose)['can_reserve']
                for purpose in (quotas if quotas is not None else [None]))
        models: dict[str, Decimal] = {}
        tasks: dict[str, Decimal] = {}
        states = {"reserved": 0, "pending": 0, "settled": 0, "unknown": 0, "released": 0}
        for task, model, status, reserve, actual in grouped:
            state = {"attempted": "pending", "uncertain": "unknown"}.get(status, status)
            states[state] += 1
            amount = _money(actual) if status == 'settled' else _money(reserve) if status in _OPEN else Decimal(0)
            models[model] = models.get(model, Decimal(0)) + amount
            tasks[task] = tasks.get(task, Decimal(0)) + amount
        committed = Decimal(summary['committed_month'])
        import calendar
        days = calendar.monthrange(now.year, now.month)[1]
        return {**summary, "hard_monthly_limit_rub": str(GLOBAL_MONTHLY_LIMIT_RUB), "calendar_timezone": "UTC",
            "remaining_rub": str(max(Decimal(0), GLOBAL_MONTHLY_LIMIT_RUB - committed)),
            "provider_remaining_rub": str(provider_available) if provider_available is not None else None,
            "dispatch_allowed": dispatch_allowed,
            "available_for_requests_rub": str(max(Decimal(0), min(GLOBAL_MONTHLY_LIMIT_RUB-committed,
                provider_available if provider_available is not None else GLOBAL_MONTHLY_LIMIT_RUB-committed))),
            "warnings": [str(GLOBAL_MONTHLY_LIMIT_RUB*Decimal(percent)/100) for percent in (50, 80, 95)
                         if committed*100 >= GLOBAL_MONTHLY_LIMIT_RUB*percent],
            "warning_percentages": [percent for percent in (50, 80, 95)
                                    if committed*100 >= GLOBAL_MONTHLY_LIMIT_RUB*percent],
            "purpose_limits_enabled": quotas is not None, "purpose_quotas": quotas or {}, "states": states,
            "soft_daily_guide_rub": str((max(Decimal(0), GLOBAL_MONTHLY_LIMIT_RUB-committed) / Decimal(days-now.day+1)).quantize(Decimal('.01'))),
            "by_model_rub": {k: str(v) for k, v in models.items()}, "by_task_rub": {k: str(v) for k, v in tasks.items()},
            "automatic_paid_search": False}

    def observe(self, kind: str, *, model: str = "", code: str = "", request_id: str = "",
                event_id: str | None = None, price_sha256: str = "", now: datetime | None = None) -> None:
        """Append idempotent content-free telemetry, never arbitrary user text."""
        ident = event_id or uuid4().hex
        values = (ident, _utc(now or datetime.now(UTC)).isoformat(), kind, model, code, request_id, price_sha256)
        if (kind not in {"attempt", "cache_hit", "local_only", "budget_refused", "model_unavailable", "provider_error",
                         "schema_refused", "tariff_observed", "debit_overrun", "outcome_unknown", "singleflight_shared", "request_binding", "answer_success"}
                or model not in {"", "qwen3.7-flash", "gpt-6-luna", "deepseek-v4-flash-0731"}
                or code not in {"", "limit", "timeout", "http", "usage", "unknown", "model", "schema", "privacy", "price"}
                or not re.fullmatch(r"[0-9a-f]{16,64}", ident)
                or (request_id and not re.fullmatch(r"[0-9a-f]{16,64}", request_id))
                or (price_sha256 and not re.fullmatch(r"[0-9a-f]{64}", price_sha256))):
            raise ValueError("Content-free telemetry contract required")
        with self._transaction() as conn:
            existing = conn.execute(f"SELECT * FROM {self.observations} WHERE event_id=?", (ident,)).fetchone()
            if existing:
                if existing[2:] != values[2:]:
                    raise BudgetReservationDenied("Conflicting telemetry replay")
                return
            conn.execute(f"INSERT INTO {self.observations} VALUES (?,?,?,?,?,?,?)", values)

    def bound_request(self, fingerprint: str) -> dict[str, str] | None:
        """Read durable request identity without changing reservations or evidence."""
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                row = conn.execute(f"SELECT r.status,r.{self.reserved},r.{self.settled} FROM {self.observations} o JOIN {self.reservations} r ON r.request_id=o.request_id WHERE o.event_id=? AND o.kind='request_binding'", (fingerprint,)).fetchone()
            return {"status": row[0], "reserved": row[1], "settled": row[2]} if row else None
        except sqlite3.Error:
            raise BudgetStoreUnavailable("Request identity unavailable") from None

    def request_status(self, request_id: str) -> dict[str, str] | None:
        """Read a reservation even if a crash preceded its binding observation."""
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                row = conn.execute(f"SELECT status,{self.reserved},{self.settled} FROM {self.reservations} WHERE request_id=?", (request_id,)).fetchone()
            return {"status": row[0], "reserved": row[1], "settled": row[2]} if row else None
        except sqlite3.Error:
            raise BudgetStoreUnavailable("Request reservation unavailable") from None

    def summary(self, *, now: datetime) -> dict[str, Any]:
        """Read settled spend and unresolved reserves without creating a journal."""
        current = _utc(now)
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                conn.execute("BEGIN")
                rows = conn.execute(f"SELECT day_utc,month_utc,{self.reserved},status,{self.settled} FROM {self.reservations}").fetchall()
                observations = conn.execute(f"SELECT observed_at,kind,model,code,price_sha256 FROM {self.observations} ORDER BY observed_at,event_id").fetchall()
                receipts = conn.execute(f"SELECT r.day_utc,r.month_utc,c.model,c.tokens_in,c.tokens_out,u.reasoning_tokens,u.cached_tokens FROM {self.reservations} r JOIN {self.receipts} c ON c.request_id=r.request_id LEFT JOIN {self.usage} u ON u.request_id=r.request_id").fetchall()
            day, month = current.date().isoformat(), current.strftime("%Y-%m")
            successes = sum(o[1] == "answer_success" for o in observations)
            failures = sum(o[1] in {"schema_refused", "debit_overrun"} for o in observations)
            settled_count = sum(r[3] == "settled" for r in rows)
            known_cost = sum((_money(r[4]) for r in rows if r[3] == "settled"), Decimal(0))
            outcomes_complete = settled_count == successes + failures and not any(r[3] in {"attempted", "uncertain"} for r in rows)
            return {"currency": self.currency, "committed_today": str(self._committed_spend(rows, day)),
                    "committed_month": str(self._committed_spend(rows, month)), "requests": len(rows),
                    "uncertain": sum(r[3] in {"attempted", "uncertain"} for r in rows),
                    "requests_today": sum(r[0] == day for r in rows), "requests_month": sum(r[1] == month for r in rows),
                    "settled_today": str(sum((_money(r[4]) for r in rows if r[0] == day and r[3] == "settled"), Decimal(0))),
                    "settled_month": str(sum((_money(r[4]) for r in rows if r[1] == month and r[3] == "settled"), Decimal(0))),
                    "unresolved_reserves": str(sum((_money(r[2]) for r in rows if r[3] in _OPEN), Decimal(0))),
                    "daily_counters": {kind: sum(o[0][:10] == day and o[1] == kind for o in observations)
                        for kind in ("attempt", "cache_hit", "local_only", "budget_refused", "model_unavailable", "provider_error", "schema_refused", "singleflight_shared")},
                    "monthly_counters": {kind: sum(o[0][:7] == month and o[1] == kind for o in observations)
                        for kind in ("attempt", "cache_hit", "local_only", "budget_refused", "model_unavailable", "provider_error", "schema_refused", "singleflight_shared")},
                    "requests_by_model": {model: sum(o[2] == model and o[1] == "attempt" for o in observations)
                        for model in sorted({o[2] for o in observations if o[1] == "attempt"})},
                    "tokens_in": sum(r[3] for r in receipts), "tokens_out": sum(r[4] for r in receipts),
                    "reasoning_tokens": sum(r[5] for r in receipts) if all(r[5] is not None for r in receipts) else None,
                    "provider_cached_tokens": sum(r[6] for r in receipts) if all(r[6] is not None for r in receipts) else None,
                    "tariff_history": [{"observed_at": o[0], "model": o[2], "price_sha256": o[4]}
                        for o in observations if o[1] == "tariff_observed"],
                    "provider_errors": [{"observed_at": o[0], "code": o[3]} for o in observations if o[1] == "provider_error"],
                    "observed_source_bound_answers": successes, "observed_failed_outputs": failures,
                    "answer_outcome_coverage": "complete" if outcomes_complete else "insufficient_data",
                    "cost_per_source_bound_answer_rub": str(known_cost / successes) if successes and outcomes_complete else None,
                    "semantic_quality_verified": False}
        except sqlite3.Error:
            raise BudgetStoreUnavailable("Budget summary unavailable") from None

    def initialize(self) -> None:
        """Explicit schema setup; never called by a budget reservation."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.reservations} (
                    request_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    day_utc TEXT NOT NULL,
                    month_utc TEXT NOT NULL,
                    {self.reserved} TEXT NOT NULL,
                    {self.settled} TEXT,
                    status TEXT NOT NULL,
                    price_evidence_sha256 TEXT NOT NULL,
                    completed_at TEXT,
                    resolution_evidence_sha256 TEXT
                )
            """)
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {self.receipts} (
                request_id TEXT PRIMARY KEY REFERENCES {self.reservations}(request_id),
                model TEXT NOT NULL, tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL,
                prompt_sha256 TEXT NOT NULL, output_sha256 TEXT NOT NULL
            )""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {self.observations} (
                event_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, kind TEXT NOT NULL,
                model TEXT NOT NULL, code TEXT NOT NULL, request_id TEXT NOT NULL, price_sha256 TEXT NOT NULL)""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {self.usage} (
                request_id TEXT PRIMARY KEY, reasoning_tokens INTEGER, cached_tokens INTEGER)""")
            conn.commit()

    def upgrade_existing(self, backup: Path) -> None:
        """Explicitly back up and extend a dedicated existing AI journal."""
        allowed = {f"{prefix}_{suffix}" for prefix in
                   ("llm_budget", "llm_rub_budget", "llm_mock_rub_budget")
                   for suffix in ("reservations", "receipts", "observations", "usage", "purpose_limits", "purposes")}
        allowed |= {'cfo_ai_policy', 'cfo_ai_tasks', 'cfo_ai_cache', 'cfo_ai_provider'}
        if backup.resolve() == self.path.resolve():
            raise ValueError("A separate new backup is required")
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {self.reservations, self.receipts} <= tables or not tables <= allowed:
                    raise BudgetStoreUnavailable("Not a dedicated existing AI journal")
                if source.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise BudgetStoreUnavailable("AI journal integrity unavailable")
                with backup.open("x"):
                    pass
                with closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
        except sqlite3.Error:
            raise BudgetStoreUnavailable("AI journal backup unavailable") from None
        self.initialize()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            uri = self.path.resolve().as_uri() + "?mode=rw"
            with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)) as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        except sqlite3.Error as exc:
            raise BudgetStoreUnavailable("Persistent LLM budget store unavailable") from exc

    @staticmethod
    def _legacy_spent(conn: sqlite3.Connection, start: datetime, epoch: datetime) -> Decimal:
        total = Decimal(0)
        for ts, cost in conn.execute("SELECT ts, cost_usd FROM llm_calls WHERE cached = 0"):
            when = datetime.fromisoformat(ts)
            when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
            if start <= when < epoch:
                total += _money(str(cost))
        return total

    @staticmethod
    def _committed_spend(rows: list[tuple[str, str, str, str, str | None]], period: str) -> Decimal:
        total = Decimal(0)
        for day, month, reserved, status, settled in rows:
            if status in _OPEN:
                total += _money(reserved)
            elif status == "settled" and (day == period or month == period):
                if settled is None:
                    raise BudgetStoreUnavailable("Invalid settled budget record")
                total += _money(settled)
            elif status != "released" and status != "settled":
                raise BudgetStoreUnavailable("Invalid budget record state")
        return total

    def _capacity(self, conn: sqlite3.Connection, amount: Decimal, day_limit: Decimal,
                  month_limit: Decimal, current: datetime, *, purpose: str | None = None) -> dict[str, Any]:
        if purpose is not None and (not isinstance(purpose, str) or purpose not in PURPOSE_LIMITS_RUB):
            raise ValueError('Unknown budget purpose')
        day, month = current.date().isoformat(), current.strftime("%Y-%m")
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)
        conn.execute(f"SELECT request_id FROM {self.receipts} LIMIT 0")
        rows = conn.execute(f"""SELECT day_utc, month_utc, {self.reserved}, status,
                               {self.settled} FROM {self.reservations}""").fetchall()
        governed = conn.execute("SELECT 1 FROM sqlite_master WHERE name='cfo_ai_policy'").fetchone()
        blocked = False
        if governed:
            month_limit = min(month_limit, GLOBAL_MONTHLY_LIMIT_RUB)
            day_limit = GLOBAL_MONTHLY_LIMIT_RUB
            blocked = any(r[3] in _OPEN or (r[4] is not None and _money(r[4]) > _money(r[2])) for r in rows)
            recent = conn.execute(f"SELECT COUNT(*) FROM {self.reservations} WHERE created_at>=? AND status!='released'",
                ((current-timedelta(minutes=1)).isoformat(),)).fetchone()[0]
            blocked = blocked or recent >= 20
            provider_available = self._provider_available(conn)
            blocked = blocked or (provider_available is not None and amount > provider_available)
        quotas = self._purpose_budgets(conn, current)
        purpose_capacity: dict[str, Any] = {}
        if quotas is not None:
            selected = purpose or 'reserve'
            quota = quotas[selected]
            blocked = blocked or amount > Decimal(quota['remaining_rub'])
            purpose_capacity = {'purpose_limits_enabled': True, 'purpose': selected,
                'purpose_limit_rub': quota['limit_rub'], 'purpose_committed_rub': quota['committed_rub'],
                'purpose_remaining_rub': quota['remaining_rub']}
        elif purpose is not None:
            blocked = True
        epoch_raw = conn.execute(
            f"SELECT MIN(created_at) FROM {self.reservations}"
        ).fetchone()[0]
        epoch = datetime.fromisoformat(epoch_raw) if epoch_raw else current
        epoch = _utc(epoch)
        day_spend = self._legacy_spent(conn, day_start, epoch) if self.currency == "USD" else Decimal(0)
        month_spend = self._legacy_spent(conn, month_start, epoch) if self.currency == "USD" else Decimal(0)
        day_spend += self._committed_spend(rows, day)
        month_spend += self._committed_spend(rows, month)
        return {**purpose_capacity, "daily_limit": str(day_limit), "monthly_limit": str(month_limit),
            "committed_today": str(day_spend), "committed_month": str(month_spend),
            "remaining_day": str(max(Decimal(0), day_limit - day_spend)),
            "remaining_month": str(max(Decimal(0), month_limit - month_spend)),
            "settled_total": str(sum((_money(r[4]) for r in rows if r[3] == 'settled'), Decimal(0))),
            "open_reservations": str(sum((_money(r[2]) for r in rows if r[3] in _OPEN), Decimal(0))),
            "unknown_reservations": str(sum((_money(r[2]) for r in rows if r[3] in {'attempted', 'uncertain'}), Decimal(0))),
            "can_reserve": not blocked and day_limit > 0 and month_limit > 0 and amount > 0 and day_spend + amount <= day_limit and month_spend + amount <= month_limit}

    def capacity(self, amount: Decimal | str, daily_limit: Decimal | str, monthly_limit: Decimal | str,
                 *, now: datetime, request_id: str | None = None, scope_identity: str | None = None,
                 purpose: str | None = None) -> dict[str, Any]:
        """Read the reservation calculation; a snapshot is never a reservation promise."""
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                conn.execute("BEGIN")
                result = self._capacity(conn, _money(amount), _money(daily_limit), _money(monthly_limit), _utc(now), purpose=purpose)
                recorded = bool(request_id and conn.execute(f"SELECT 1 FROM {self.reservations} WHERE request_id=?", (request_id,)).fetchone())
                bound = bool(scope_identity and conn.execute(f"SELECT 1 FROM {self.observations} WHERE event_id=?", (scope_identity,)).fetchone())
                return {**result, "can_reserve": result['can_reserve'] and not recorded and not bound,
                    "request_recorded": recorded or bound, "atomic_reservation_required": True}
        except sqlite3.Error:
            raise BudgetStoreUnavailable("Budget capacity unavailable") from None

    def reserve(
        self, request_id: str, amount_usd: Decimal | float | str,
        daily_limit_usd: Decimal | float | str, monthly_limit_usd: Decimal | float | str,
        price_evidence_sha256: str, *, now: datetime, scope_identity: str | None = None,
        purpose: str | None = None,
    ) -> None:
        """Reserve worst-case spend atomically; duplicate request IDs deny replay."""
        current = _utc(now)
        if scope_identity is not None and (self.currency != "RUB" or not re.fullmatch(r"[0-9a-f]{64}", scope_identity)):
            raise ValueError("Invalid invocation scope")
        amount = _money(amount_usd)
        day_limit = _money(daily_limit_usd)
        month_limit = _money(monthly_limit_usd)
        if (not request_id or amount <= 0 or day_limit <= 0 or month_limit <= 0 or
                len(price_evidence_sha256) != 64):
            raise ValueError("invalid reservation contract")
        day = current.date().isoformat()
        month = current.strftime("%Y-%m")
        with self._transaction() as conn:
            if scope_identity is not None and conn.execute(f"SELECT 1 FROM {self.observations} WHERE event_id=?", (scope_identity,)).fetchone():
                raise BudgetReservationDenied("Owner grant invocation already reserved; no replay")
            capacity = self._capacity(conn, amount, day_limit, month_limit, current, purpose=purpose)
            if not capacity['can_reserve']:
                raise BudgetReservationDenied("LLM budget would be exceeded")
            try:
                conn.execute(f"""INSERT INTO {self.reservations}
                    (request_id, created_at, day_utc, month_utc, {self.reserved},
                     {self.settled}, status, price_evidence_sha256, completed_at,
                     resolution_evidence_sha256)
                    VALUES (?, ?, ?, ?, ?, NULL, 'reserved', ?, NULL, NULL)""",
                    (request_id, current.isoformat(), day, month, str(amount), price_evidence_sha256))
            except sqlite3.IntegrityError as exc:
                raise BudgetReservationDenied("Duplicate LLM request reservation") from exc
            if capacity.get('purpose_limits_enabled'):
                conn.execute(f'INSERT INTO {self.purposes} VALUES(?,?)', (request_id, purpose or 'reserve'))
            if scope_identity is not None:
                conn.execute(f"INSERT INTO {self.observations} VALUES(?,?,?,?,?,?,?)",
                    (scope_identity, current.isoformat(), "request_binding", "qwen3.7-flash", "", request_id, price_evidence_sha256))

    def ensure_attribution(self) -> None:
        with self._transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS llm_rub_budget_attribution(
                request_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, job_id TEXT NOT NULL,
                created_at TEXT NOT NULL)""")

    def attribute(self, request_id: str, role_id: str, job_id: str, *,
                  now: datetime | None = None) -> None:
        """Attach a role/job to a reservation once; legacy rows stay unattributed."""
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", role_id or ""):
            raise ValueError("Invalid role id")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id or ""):
            raise ValueError("Invalid job id")
        self.ensure_attribution()
        with self._transaction() as conn:
            conn.execute("""INSERT OR IGNORE INTO llm_rub_budget_attribution
                VALUES (?,?,?,?)""", (request_id, role_id, job_id,
                (now or datetime.now(UTC)).isoformat()))

    def role_spend(self, *, now: datetime) -> dict[str, dict[str, str]]:
        """Monthly settled/reserved/uncertain per role; legacy rows are separate."""
        self.ensure_attribution()
        month = _utc(now).strftime("%Y-%m")
        with self._transaction() as conn:
            rows = conn.execute(f"""SELECT a.role_id, r.status, r.{self.reserved}, r.{self.settled}
                FROM {self.reservations} r LEFT JOIN llm_rub_budget_attribution a
                ON a.request_id=r.request_id WHERE r.month_utc=?""", (month,)).fetchall()
        report: dict[str, dict[str, str]] = {}
        for role, status, reserved, settled in rows:
            key = role or 'unattributed'
            item = report.setdefault(key, {'settled': '0', 'reserved': '0', 'uncertain': '0'})
            if status == 'settled':
                item['settled'] = str(Decimal(item['settled']) + Decimal(settled or 0))
            elif status in {'attempted', 'uncertain'}:
                item['uncertain'] = str(Decimal(item['uncertain']) + Decimal(reserved or 0))
            elif status == 'reserved':
                item['reserved'] = str(Decimal(item['reserved']) + Decimal(reserved or 0))
        return report

    def mark_attempted(self, request_id: str, *, model: str | None = None) -> None:
        """Durably mark a request before any provider attempt."""
        self._transition(request_id, "reserved", "attempted", model=model)

    def mark_uncertain(self, request_id: str) -> None:
        """Keep the full reserve after an ambiguous provider outcome."""
        self._transition(request_id, "attempted", "uncertain")

    def release(self, request_id: str) -> None:
        """Release only when no provider attempt has started."""
        self._transition(request_id, "reserved", "released")

    def _transition(self, request_id: str, expected: str, target: str, *, model: str | None = None) -> None:
        if model is not None and model not in {"qwen3.7-flash", "qwen3.7-plus", "gemini-2.5-flash-lite", "gpt-6-luna", "deepseek-v4-flash-0731"}:
            raise ValueError("Pinned telemetry model required")
        with self._transaction() as conn:
            result = conn.execute(f"""UPDATE {self.reservations} SET status = ?
                WHERE request_id = ? AND status = ?""", (target, request_id, expected))
            if result.rowcount != 1:
                raise BudgetReservationDenied("Reservation state does not permit transition")
            if model is not None:
                price = conn.execute(f"SELECT price_evidence_sha256 FROM {self.reservations} WHERE request_id=?", (request_id,)).fetchone()[0]
                conn.execute(f"INSERT INTO {self.observations} VALUES (?,?,?,?,?,?,?)",
                    (hashlib.sha256((request_id + ':attempt').encode()).hexdigest(), datetime.now(UTC).isoformat(),
                     "attempt", model, "", request_id, price))

    def settle(self, request_id: str, actual_usd: Decimal | float | str, *, now: datetime,
               resolution_evidence_sha256: str | None = None,
               receipt: UsageReceipt | None = None, reasoning_tokens: int | None = None,
               cached_tokens: int | None = None) -> None:
        """Settle once, allowing identical replay but rejecting conflicting cost."""
        actual = _money(actual_usd)
        current = _utc(now)
        receipt_values = receipt.values() if receipt is not None else None
        if ((reasoning_tokens is not None and (receipt is None or type(reasoning_tokens) is not int or not 0 <= reasoning_tokens <= receipt.tokens_out))
                or (cached_tokens is not None and (receipt is None or type(cached_tokens) is not int or not 0 <= cached_tokens <= receipt.tokens_in))):
            raise ValueError("Invalid usage metadata")
        with self._transaction() as conn:
            row = conn.execute(f"""SELECT status, {self.settled} FROM {self.reservations}
                WHERE request_id = ?""", (request_id,)).fetchone()
            if row is None:
                raise BudgetReservationDenied("Unknown LLM reservation")
            if row[0] == "settled" and row[1] is not None and _money(row[1]) == actual:
                if receipt_values is not None:
                    existing = conn.execute(f"""SELECT model,tokens_in,tokens_out,
                        prompt_sha256,output_sha256 FROM {self.receipts}
                        WHERE request_id = ?""", (request_id,)).fetchone()
                    if existing != receipt_values:
                        raise BudgetReservationDenied("Conflicting usage receipt replay")
                if reasoning_tokens is not None or cached_tokens is not None:
                    metadata = conn.execute(f"SELECT reasoning_tokens,cached_tokens FROM {self.usage} WHERE request_id=?", (request_id,)).fetchone()
                    if metadata != (reasoning_tokens, cached_tokens):
                        raise BudgetReservationDenied("Conflicting usage metadata replay")
                return
            if row[0] not in {"attempted", "uncertain"}:
                raise BudgetReservationDenied("Reservation cannot be settled")
            if row[0] == "uncertain" and (
                resolution_evidence_sha256 is None or len(resolution_evidence_sha256) != 64
            ):
                raise BudgetReservationDenied("Uncertain cost requires resolution evidence")
            if receipt_values is not None:
                conn.execute(f"INSERT INTO {self.receipts} VALUES (?, ?, ?, ?, ?, ?)",
                             (request_id, *receipt_values))
                conn.execute(f"INSERT INTO {self.usage} VALUES (?,?,?)", (request_id, reasoning_tokens, cached_tokens))
            conn.execute(f"""UPDATE {self.reservations} SET status = 'settled',
                {self.settled} = ?, completed_at = ?, resolution_evidence_sha256 = ?
                WHERE request_id = ?""",
                (str(actual), current.isoformat(), resolution_evidence_sha256, request_id))

    def recover_stale(self, *, now: datetime, after: timedelta) -> tuple[int, int]:
        """Release unattempted work; retain attempted cost as uncertain."""
        current = _utc(now)
        if after <= timedelta(0):
            raise ValueError("recovery interval must be positive")
        released = uncertain = 0
        with self._transaction() as conn:
            for request_id, created, status in conn.execute(
                f"SELECT request_id, created_at, status FROM {self.reservations}"
            ):
                if current - _utc(datetime.fromisoformat(created)) < after:
                    continue
                if status == "reserved":
                    conn.execute(f"UPDATE {self.reservations} SET status = 'released' WHERE request_id = ?",
                                 (request_id,))
                    released += 1
                elif status == "attempted":
                    conn.execute(f"UPDATE {self.reservations} SET status = 'uncertain' WHERE request_id = ?",
                                 (request_id,))
                    uncertain += 1
        return released, uncertain


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Create a NEW dedicated RUB LLM journal; no API calls")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upgrade-existing", action="store_true")
    parser.add_argument("--backup", type=Path)
    arguments = parser.parse_args()
    if arguments.upgrade_existing:
        if arguments.backup is None:
            parser.error("--upgrade-existing requires --backup NEW-path")
        BudgetStore(arguments.output, currency="RUB").upgrade_existing(arguments.backup)
        raise SystemExit(0)
    # Existing financial databases/profiles can never be selected for initialization.
    with arguments.output.open("x"):
        pass
    BudgetStore(arguments.output, currency="RUB").initialize()
