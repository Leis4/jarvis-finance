"""Personal CFO over the existing ledger, with explicit source and uncertainty."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from statistics import median
from threading import Lock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jarvis.finance.db import FinanceDB
from jarvis.finance.domain import Transaction
from jarvis.finance.event_taxonomy import NON_ECONOMIC_CATEGORIES, canonical_category
from jarvis.finance.ledger import cashflow, conflicting_identities, direction_conflict
from jarvis.finance.personal_profile import (
    PersonalProfile,
    business_date,
    exact_money,
    financial_decision,
    goal_view,
)

ZERO = Decimal(0)
CENT = Decimal("0.01")


money = exact_money


def backup_database(db: FinanceDB, directory: Path) -> Path:
    """Create a consistent backup with a verified disposable restore receipt."""
    from jarvis.finance.personal_recovery import create_backup
    return create_backup(db, directory)


class PersonalPlan(BaseModel):
    """Owner assumptions, deliberately separate from bank observations."""
    model_config = ConfigDict(extra="forbid")
    cash_low: str = "0.00"
    cash_high: str = "0.00"
    reserve: str | None = None
    obligations: str | None = None
    next_income_date: date | None = None
    next_income_amount: str | None = None
    goal: str = Field(default="", max_length=200)
    goal_amount: str | None = None

    @model_validator(mode="after")
    def validate_amounts(self) -> PersonalPlan:
        for value in (self.cash_low, self.cash_high, self.reserve, self.obligations,
                      self.next_income_amount, self.goal_amount):
            if value is not None and money(value) < 0:
                raise ValueError("Суммы плана не могут быть отрицательными.")
        if money(self.cash_low) > money(self.cash_high):
            raise ValueError("Неверный диапазон наличных.")
        return self


class IngestRejected(ValueError):
    """Owner-safe, static reason why a bank capture was refused.

    The reason is a fixed token, never bank data: it exists so the UI and the
    local diagnostics can name the failing check without leaking anything.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PersonalCFO:
    """Reuse FinanceDB; never treat ledger movement as current bank balance."""

    def __init__(self, db: FinanceDB):
        self.db = db
        self.lock = Lock()

    def plan(self) -> PersonalPlan:
        return PersonalPlan.model_validate(self.db.get_setting("cfo.plan", {}))

    def save_plan(self, plan: PersonalPlan) -> None:
        if self.db.get_setting("cfo.profile.v2", None) is not None:
            raise ValueError("Используйте личный профиль v2; старый план не заменяет его данные.")
        self.db.set_setting("cfo.plan", plan.model_dump(mode="json"))

    def profile(self) -> PersonalProfile:
        return PersonalProfile.model_validate(self.db.get_setting("cfo.profile.v2", {}))

    def bank_snapshot(self) -> dict[str, Any] | None:
        """Use only a snapshot bound to the currently committed source capture."""
        snapshot = self.db.get_setting("cfo.bank_snapshot", None)
        capture = self.db.get_setting("cfo.last_capture", None)
        if not snapshot or not capture:
            return None
        capture_sha = hashlib.sha256(json.dumps(capture, sort_keys=True).encode()).hexdigest()
        return snapshot if snapshot.get("capture_sha256") == capture_sha else None

    def save_profile(self, profile: PersonalProfile) -> None:
        previous = self.profile() if self.db.get_setting("cfo.profile.v2", None) is not None else None
        stamped = profile.stamped(datetime.now(UTC), previous).model_dump(mode="json")
        history = self.db.get_setting("cfo.profile_history", [])
        if previous and previous.model_dump(mode="json") not in history:
            history.append(previous.model_dump(mode="json"))
        history.append(stamped)
        with self.db.connect() as conn:
            for key, value in (("cfo.profile_history", history), ("cfo.profile.v2", stamped)):
                conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))
            self.db.audit(conn, "cfo_profile_committed", {"history_entries": len(history)})

    def goals(self, today: date) -> list[dict[str, Any]]:
        return [goal_view(g, self.db.get_setting("cfo.goal_plan:" + str(g["id"]), {}), today)
                for g in self.db.list_savings_goals(status=None)]

    def archive_capture(self, capture: dict[str, Any]) -> None:
        from jarvis.finance.personal_reconciliation import identity
        directory = self.db.path.parent / "cfo-captures"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (identity(capture) + ".json")
        content = json.dumps(capture, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                raise ValueError("Capture archive integrity mismatch")
        else:
            with path.open("x", encoding="utf-8") as target:
                target.write(content)

    def correct(self, tx_id: int, category: str, expected_version: int | None = None) -> bool:
        from jarvis.finance.personal_product import validated_category
        category = validated_category(category)
        # Existing correction provenance is the ground truth; no global broad rule.
        if expected_version is not None:
            from jarvis.finance.personal_product import confirm_if_version
            saved = confirm_if_version(self.db, tx_id, category, expected_version)
        else:
            saved = self.db.confirm_transaction_category(tx_id, category)
        return saved

    def confirmed_categories(self, merchant: str, amount: Decimal | None = None, currency: str = "RUB") -> set[str]:
        if not merchant:
            return set()
        with self.db.connect() as conn:
            rows = conn.execute("""SELECT DISTINCT t.category,t.amount,t.currency FROM transactions t
                JOIN category_corrections c ON c.transaction_id=t.id
                WHERE t.merchant=? AND c.source='manual' AND c.version=(
                    SELECT MAX(c2.version) FROM category_corrections c2 WHERE c2.transaction_id=t.id)
                AND c.new_category=t.category""", (merchant,)).fetchall()
        return {str(r[0]) for r in rows if r[2] == currency and (amount is None or (money(r[1]) < 0) == (amount < 0))}

    def learned_category(self, merchant: str) -> str | None:
        from jarvis.finance.personal_product import marketplace
        if marketplace(merchant):
            return None
        categories = self.confirmed_categories(merchant)
        return next(iter(categories)) if len(categories) == 1 else None

    def category(self, merchant: str, description: str, amount: Decimal | None = None, currency: str = "RUB") -> str:
        from jarvis.finance.personal_product import marketplace
        categories = self.confirmed_categories(merchant, amount, currency)
        if marketplace(merchant):
            categories = set()
        if categories:
            return next(iter(categories)) if len(categories) == 1 else "pending_review"
        if amount is not None and amount > 0 and re.search(r"(?i)возврат (?:по )?(?:покупк|товар)|\brefund\b", description):
            original = self.confirmed_categories(merchant, -amount, currency)
            if marketplace(merchant):
                original = set()
            ordinary = original - NON_ECONOMIC_CATEGORIES - {"pending_review", "unknown", "uncategorized"}
            return "refund:" + next(iter(ordinary)) if len(ordinary) == 1 else "refund"
        rule = self.db.find_merchant_rule(description)
        if rule:
            return canonical_category(rule)
        from jarvis.finance.classification.layers import l5_merchant_kb
        candidate = l5_merchant_kb({"merchant": merchant, "description_clean": description})
        return candidate.category if candidate and candidate.confidence >= 0.9 and (amount is None or amount < 0) else "pending_review"

    def refresh(self, *, allow_pin: bool = True, pin: str | None = None) -> dict[str, Any]:
        """One bounded read capture; fail without substituting fixtures or old facts."""
        if not self.lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            from jarvis.finance.personal_bank import capture_bank
            now = datetime.now(UTC)
            last = self.db.get_setting("cfo.last_refresh", {}) or {}
            # Never hammer the bank. A failed sign-in cools down, and repeated
            # failures extend it, so a wrong PIN can never turn into a lockout loop.
            cooldown = self._auth_cooldown(last, now, explicit_pin=pin is not None)
            if cooldown is not None:
                return {"status": "cooling_down", "code": "auth_cooldown",
                        "retry_after_seconds": int(cooldown), "attempted_at": last.get("attempted_at")}
            today = business_date(now)
            previous = self.db.get_setting("cfo.last_capture", None)
            if previous:
                self.archive_capture(previous)
            options: dict[str, Any] = {'previous': previous} if previous else {}
            if not allow_pin:
                options['allow_pin'] = False
            if pin is not None:
                options['pin'] = pin
            capture = capture_bank(today - timedelta(days=90), today, **options)
            if capture["status"] != "captured":
                capture["attempted_at"] = now.isoformat()
                if capture.get("status") == "auth_required":
                    capture["auth_failures"] = int(last.get("auth_failures") or 0) + 1
                self.db.set_setting("cfo.last_refresh", capture)
                return capture
            try:
                # The capture is stamped after the SDK round-trip, so it is always
                # newer than `now` taken at the start of this method. Ingest must be
                # given the current moment, otherwise the future-snapshot guard
                # rejects every real capture.
                return self.ingest(capture, now=datetime.now(UTC))
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                failure: dict[str, Any] = {"status": "unavailable", "code": type(exc).__name__,
                                           "attempted_at": now.isoformat()}
                if isinstance(exc, IngestRejected):
                    failure = {"status": "unavailable", "code": "ingest_rejected",
                               "reason": exc.reason, "attempted_at": now.isoformat()}
                # Keep the refused capture locally so the exact check can be
                # reproduced offline. Same private archive as accepted captures.
                try:
                    self.archive_capture(capture)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                self.db.set_setting("cfo.last_refresh", failure)
                self._record_diagnostic("ingest", failure)
                return failure
        finally:
            self.lock.release()

    @staticmethod
    def _record_diagnostic(stage: str, payload: dict[str, Any]) -> None:
        """Append a tiny, secret-free diagnostic line; bounded and best effort."""
        import os
        from pathlib import Path as _Path

        root = _Path("data/cfo-review")
        try:
            root.mkdir(parents=True, exist_ok=True)
            path = root / "sber-diagnostics.jsonl"
            entry = {"at": datetime.now(UTC).isoformat(), "stage": stage,
                     "code": payload.get("code"), "reason": payload.get("reason")}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + os.linesep)
            if path.stat().st_size > 64_000:
                lines = path.read_text(encoding="utf-8").splitlines()[-200:]
                path.write_text(os.linesep.join(lines) + os.linesep, encoding="utf-8")
        except (OSError, ValueError):
            pass

    @staticmethod
    def _auth_cooldown(last: dict[str, Any], now: datetime, *, explicit_pin: bool) -> float | None:
        """Seconds left before another sign-in attempt is allowed, if any."""
        if last.get("status") != "auth_required" or not last.get("attempted_at"):
            return None
        try:
            attempted = datetime.fromisoformat(last["attempted_at"])
        except (TypeError, ValueError):
            return None
        if attempted.tzinfo is None:
            return None
        failures = int(last.get("auth_failures") or 0)
        window = 60 if failures <= 1 else 300 if failures == 2 else 1800
        if explicit_pin:
            window = min(window, 60)
        elapsed = (now - attempted).total_seconds()
        return max(0.0, window - elapsed) or None

    def ensure_bank_current(self) -> None:
        """Refresh stale facts once on a question, cooling down failed restoration."""
        now = datetime.now(UTC)
        snapshot = self.bank_snapshot()
        if snapshot and timedelta(0) <= now - datetime.fromisoformat(snapshot["observed_at"]) <= timedelta(minutes=15):
            return
        attempt = self.db.get_setting("cfo.last_refresh", {})
        if attempt.get("attempted_at") and now - datetime.fromisoformat(attempt["attempted_at"]) < timedelta(minutes=5):
            return
        self.refresh()

    def ingest(self, capture: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        """Accept exact account-scoped events; hold ambiguous card and state data."""
        if now.tzinfo is None or capture.get("status") != "captured":
            raise IngestRejected("capture_not_timestamped")
        products, history = capture["products"], capture["history"]
        capture_sha = hashlib.sha256(json.dumps(capture, sort_keys=True).encode()).hexdigest()
        previous = self.db.get_setting("cfo.bank_snapshot", {})
        observed = datetime.fromisoformat(capture.get("observed_at", now.isoformat()))
        if previous.get("capture_sha256") == capture_sha:
            observed = datetime.fromisoformat(previous["observed_at"])
        if observed.tzinfo is None or observed > now:
            raise IngestRejected("observation_clock_invalid")
        if previous and observed < datetime.fromisoformat(previous["observed_at"]):
            raise IngestRejected("capture_older_than_snapshot")
        start, end = date.fromisoformat(history["scope"]["start"]), date.fromisoformat(history["scope"]["end"])
        if start > end or end > business_date(now):
            raise IngestRejected("capture_period_invalid")
        accounts: dict[str, dict[str, Any]] = {}
        resources: dict[str, str] = {}
        for item in products["accounts"]:
            ident = str(item["id"])
            if ident in accounts:
                raise IngestRejected("duplicate_account_inventory")
            balance = item["balance"]
            amount = money(balance["amount"])
            currency = balance["currency"]
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise IngestRejected("currency_invalid")
            accounts[ident] = {"account": ident, "amount": str(amount), "currency": currency,
                              "restricted": item.get("arrested") is True or item.get("hidden") is True}
            resource = item.get("operation_resource")
            if resource:
                if resource in resources:
                    raise IngestRejected("operation_resource_ambiguous")
                resources[resource] = ident
        if not accounts:
            raise IngestRejected("no_observed_accounts")
        from jarvis.finance.personal_reconciliation import normalize_capture
        normalization = normalize_capture(capture, self.db.all_transactions())
        transactions: list[Transaction] = []
        for record in normalization["records"]:
            category = ("internal_transfer" if record["internal_transfer_proven"] else
                        self.category(record["merchant"], record["description"], money(record["amount"]), record["currency"]))
            transactions.append(Transaction(date.fromisoformat(record["date"]), money(record["amount"]),
                record["currency"], record["description"], record["merchant"], category, record["account"], record["native_id"]))
        self.archive_capture(capture)
        held = normalization["held"]
        # Persist raw financial evidence locally, excluding session and credentials.
        snapshot = {"observed_at": observed.isoformat(), "capture_sha256": capture_sha, "accounts": list(accounts.values()),
            "source": "bank_observed", "history_coverage": history.get("coverage", "unknown"),
            "coverage_basis": "requested_visible_history_only", "held_operations": held,
            "period": [history["scope"]["start"], history["scope"]["end"]],
            "bank_reconciled": False}
        snapshots = self.db.get_setting("cfo.bank_snapshots", [])
        if previous and not snapshots:
            snapshots.append(previous)
        if not any(s.get("capture_sha256") == capture_sha for s in snapshots):
            snapshots.append(snapshot)
        # Ledger, evidence pointers and completion receipt commit together. The
        # immutable archive may survive a rollback; replay remains idempotent.
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = sum(self.db._insert_transaction(conn, tx, "sber_mcp", "personal-cfo-v1") for tx in transactions)
            result = {"status": "updated", "inserted": inserted, "held": held,
                      "bank_reconciled": False, "observed_at": observed.isoformat()}
            values = {"cfo.bank_snapshot": snapshot, "cfo.last_capture": capture,
                "cfo.normalization_receipt": {k: v for k, v in normalization.items() if k != "records"},
                "cfo.bank_snapshots": snapshots, "cfo.last_refresh": result}
            for key, value in values.items():
                conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))
            self.db.audit(conn, "cfo_capture_committed", {"capture_sha256": capture_sha, "inserted": inserted, "held": held})
        return result

    def economic_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply structural transfer evidence to views; preserve original rows and human edits."""
        from jarvis.finance.personal_reconciliation import normalize_capture
        all_rows = self.db.all_transactions()
        conflicts = conflicting_identities(all_rows)
        rows = [{**r, 'identity_conflict': True}
            if r.get('account') and r.get('external_id')
                and (str(r['account']), str(r['external_id'])) in conflicts else r for r in rows]
        capture = self.db.get_setting("cfo.last_capture", None)
        if not capture or not self.bank_snapshot():
            return rows
        normalized = normalize_capture(capture, all_rows)
        transfers = {(r["account"], r["native_id"]) for r in normalized["records"] if r["internal_transfer_proven"]}
        with self.db.connect() as conn:
            manual = {r[0] for r in conn.execute("SELECT DISTINCT transaction_id FROM category_corrections c WHERE source='manual' AND version=(SELECT MAX(c2.version) FROM category_corrections c2 WHERE c2.transaction_id=c.transaction_id)")}
        return [{**r, "category": "internal_transfer", "category_origin": "bank_resource_proof"}
                if (r["account"], r.get("external_id")) in transfers and r["id"] not in manual else r for r in rows]

    def data_status(self) -> dict[str, Any]:
        from jarvis.finance.personal_reconciliation import data_status
        capture = self.db.get_setting("cfo.last_capture", None)
        if not capture:
            return {"status": "unavailable", "accounts": [], "held": None}
        if not self.bank_snapshot():
            return {"status": "unavailable", "reason": "capture_snapshot_mismatch", "accounts": [], "held": None}
        snapshots = self.db.get_setting("cfo.bank_snapshots", [])
        if not snapshots:
            snapshots = [self.db.get_setting("cfo.bank_snapshot", {})]
        return data_status(self.db, capture, snapshots)

    def dashboard(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        today = business_date(now)
        snapshot = self.bank_snapshot()
        rows = self.economic_rows(self.db.transactions_between(today - timedelta(days=89), today))
        flow = cashflow(rows)
        # Unknown negatives count conservatively toward spending; unknown positives
        # never become predicted salary. Older imported history is not full coverage.
        daily: dict[date, Decimal] = defaultdict(lambda: ZERO)
        for row in rows:
            if row["currency"] == "RUB" and money(row["amount"]) < 0:
                cat = canonical_category(str(row["category"]))
                if cat not in NON_ECONOMIC_CATEGORIES:
                    daily[date.fromisoformat(row["tx_date"])] += abs(money(row["amount"]))
        rate = (sum(daily.values(), ZERO) / Decimal(90)).quantize(CENT)
        bank_total: Decimal | None = None
        fresh = False
        if snapshot:
            age = now - datetime.fromisoformat(snapshot["observed_at"])
            fresh = timedelta(0) <= age <= timedelta(minutes=15)
            bank_total = sum((money(a["amount"]) for a in snapshot["accounts"] if a["currency"] == "RUB"), ZERO)
        plan = self.plan()
        base = bank_total + money(plan.cash_low) if bank_total is not None else None
        forecasts = []
        for days in (7, 14, 30):
            end = today + timedelta(days=days)
            income = money(plan.next_income_amount) if (plan.next_income_amount is not None and
                plan.next_income_date is not None and today < plan.next_income_date <= end) else ZERO
            estimate = (base - rate * days - money(plan.obligations) + income
                        if base is not None and plan.obligations is not None else None)
            forecasts.append({"days": days, "amount": str(estimate) if estimate is not None else None,
                "kind": "experimental_scenario", "fresh_bank": fresh,
                "missing": ["owner_obligations"] if plan.obligations is None else [],
                "assumptions": "90 calendar days, possibly incomplete; all owner obligations charged; only explicit next income"})
        decision = financial_decision(snapshot, self.profile(), rows, self.goals(today), now=now)
        patterns = self.patterns(rows)
        reference_ids = {ident for pattern in patterns
            for ident in pattern.get('transaction_ids', [pattern.get('transaction_id')])}
        return {"status": "available" if fresh else "insufficient_data", "bank": snapshot,
            "bank_rub": str(bank_total) if bank_total is not None else None, "bank_fresh": fresh,
            "owner_plan": plan.model_dump(mode="json"), "last_refresh": self.db.get_setting("cfo.last_refresh", None),
            "history_rows": len(rows), "history_coverage": snapshot["history_coverage"] if snapshot else "unknown",
            "cashflow": json.loads(json.dumps(flow, default=str)), "daily_spend_baseline": str(rate),
            "forecast": forecasts, "patterns": patterns, "transactions": rows[-200:][::-1],
            "pattern_rows": [{'id': row['id'], 'tx_date': row['tx_date']} for row in rows if row['id'] in reference_ids],
            "profile": self.profile().model_dump(mode="json"), "financial_position": decision,
            "limitations": ["Bank account subtotal, not all assets; cards are not added twice.",
                "History is not reconciled to opening balances; forecasts are scenarios, not guaranteed funds."]}

    def patterns(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Expose existing recurrence criteria and median anomalies as candidates."""
        from jarvis.finance.insights.deterministic import (
            InsightPolicy,
            recurring_pattern,
        )
        policy = InsightPolicy()
        groups: dict[tuple[str, bool, str], list[dict[str, Any]]] = defaultdict(list)
        peers: dict[str, list[Decimal]] = defaultdict(list)
        cards = []
        for row in rows:
            category = canonical_category(row["category"])
            if row["currency"] != "RUB" or category in NON_ECONOMIC_CATEGORIES | {"pending_review", "uncategorized", "unknown"}:
                continue
            amount = money(row["amount"])
            if not amount or row.get('identity_conflict') or direction_conflict(amount, category):
                continue
            merchant = str(row.get("merchant") or "")
            if merchant:
                groups[(merchant, amount > 0, category)].append({"date": row["tx_date"],
                    "transaction_id": row["id"], "net_expense": str(abs(amount))})
            if amount < 0:
                baseline = median(peers[category]) if len(peers[category]) >= policy.minimum_baseline_observations else None
                if (baseline is not None and baseline > 0 and abs(amount) >= baseline * policy.unusual_ratio
                        and abs(amount) - baseline >= policy.minimum_change):
                    cards.append({"kind": "unusual_expense_candidate", "amount": str(abs(amount)),
                        "category": category, "transaction_id": row["id"], "confirmed": False})
                peers[category].append(abs(amount))
        for (merchant, income, category), group in sorted(groups.items()):
            pattern = recurring_pattern(group, policy)
            if pattern:
                cards.append({"kind": "income_candidate" if income else "expense_candidate", "merchant": merchant,
                    "category": category, **pattern, "confirmed": False, "included_in_forecast": False})
            elif not income and len(group) > policy.minimum_observations:
                ordered = sorted(group, key=lambda row: (row['date'], row['transaction_id']))
                baseline_pattern = recurring_pattern(ordered[:-1], policy)
                if baseline_pattern:
                    previous = money(baseline_pattern['typical_net_expense'])
                    current = money(ordered[-1]['net_expense'])
                    gap = (date.fromisoformat(ordered[-1]['date']) - date.fromisoformat(ordered[-2]['date'])).days
                    if (6 <= gap <= 45 and baseline_pattern['gap_min_days'] - policy.recurring_gap_variation_days <= gap
                            <= baseline_pattern['gap_max_days'] + policy.recurring_gap_variation_days
                            and abs(current - previous) >= policy.minimum_change
                            and abs(current - previous) * 100 >= previous * policy.relative_change_percent):
                        cards.append({'kind': 'recurring_price_change_candidate', 'merchant': merchant,
                            'category': category, 'previous_amount': str(previous), 'amount': str(current),
                            'transaction_ids': [row['transaction_id'] for row in ordered][-20:],
                            'confirmed': False, 'included_in_forecast': False})
        return cards[-12:]

    def purchase(self, price: str, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        cost = money(price)
        if cost <= 0:
            raise ValueError("Цена должна быть положительной.")
        if self.db.get_setting("cfo.profile.v2", None) is not None:
            rows = self.economic_rows(self.db.transactions_between(business_date(now) - timedelta(days=89), business_date(now)))
            result = financial_decision(self.bank_snapshot(), self.profile(), rows,
                self.goals(business_date(now)), now=now, price=cost)
            labels = {"insufficient_data": "Предварительный расчёт: данных для окончательного решения недостаточно",
                "can_buy": "Можно покупать в рамках указанных предположений", "wait": "Лучше подождать",
                "risk_shortfall": "Покупка создаёт риск нехватки средств"}
            text = labels[result["status"]] + f". Цена {cost:.2f} ₽. "
            if result["after_purchase_low"] is not None:
                text += f"Ликвидных средств после покупки: {result['after_purchase_low']}–{result['after_purchase_high']} ₽. "
            if result["missing"]:
                text += "Для окончательного решения нужны: " + ", ".join(result["missing"]) + ". "
            text += "Ожидаемые доходы не гарантированы; вероятность нехватки не оценена из-за недостаточной проверенной статистики."
            return {**result, "text": text, "price": str(cost), "sources": {
                "bank": "observed_bank", "profile": "dated_owner_assumptions", "forecasts": "conditional_scenarios"}}
        data, plan = self.dashboard(now=now), self.plan()
        missing = []
        if not data["bank_fresh"]:
            missing.append("свежие банковские остатки")
        if any(a.get("restricted") for a in (data["bank"] or {}).get("accounts", [])):
            missing.append("ликвидность счетов с ограничениями")
        if plan.reserve is None:
            missing.append("размер неприкосновенного резерва")
        if plan.obligations is None:
            missing.append("подтверждённые обязательные платежи до поступления")
        if plan.next_income_date is None or plan.next_income_date <= business_date(now):
            missing.append("дата следующего поступления")
        if data["history_coverage"] != "complete" or data["history_rows"] == 0:
            missing.append("достаточная история расходов")
        low = high = safe = None
        if data["bank_rub"] is not None:
            bank = money(data["bank_rub"])
            low, high = bank + money(plan.cash_low) - cost, bank + money(plan.cash_high) - cost
        days = max(1, (plan.next_income_date - business_date(now)).days) if plan.next_income_date else None
        budget = None
        if low is not None and days is not None and plan.reserve is not None and plan.obligations is not None:
            # Reserve and obligations are protected before discretionary spending.
            available = money(data["bank_rub"]) + money(plan.cash_low) - money(plan.reserve) - money(plan.obligations)
            budget = {str(n): str((max(ZERO, available) / days * min(n, days)).quantize(CENT, rounding=ROUND_DOWN)) for n in (1, 7, 30)}
            safe = low - money(plan.reserve) - money(plan.obligations) - money(data["daily_spend_baseline"]) * days
        recommendation = "insufficient_data" if missing else "defer" if safe is None or safe < 0 else "within_stated_assumptions"
        text = f"Покупка за {cost:.2f} ₽. "
        text += (f"После покупки останется {low:.2f}–{high:.2f} ₽ с учётом указанной наличности. "
                 if low is not None and high is not None else "Текущие банковские средства неизвестны. ")
        text += ("Для решения нужны: " + ", ".join(missing) + "." if missing else
                 "Лучше отложить покупку: расчёт затрагивает необходимые средства." if recommendation == "defer" else
                 "Покупка помещается в указанные предположения; прогноз расходов не гарантирован.")
        return {"status": recommendation, "text": text, "price": str(cost), "after_purchase_low": str(low) if low is not None else None,
            "after_purchase_high": str(high) if high is not None else None, "after_protection_and_baseline": str(safe) if safe is not None else None,
            "budget_before_purchase_1_7_30_days": budget, "missing": missing,
            "sources": {"bank": "bank_observed" if data["bank_fresh"] else "unavailable_or_stale",
                "cash_reserve_obligations": "owner_input", "spending": "experimental_90_day_baseline"}}
