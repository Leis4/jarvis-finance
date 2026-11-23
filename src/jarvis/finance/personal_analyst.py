"""Bounded personal explanations using existing RUB journal and fact templates."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from jarvis.core.llm.budget_store import BudgetStore
from jarvis.core.llm.providers.openai_compat import OpenAICompatProvider
from jarvis.finance.event_taxonomy import NON_ECONOMIC_CATEGORIES
from jarvis.finance.financial_analyst import (
    ReasoningCandidate,
    checked_reasoning,
    reasoning_prompt,
    render_reasoning,
)
from jarvis.finance.ledger import cashflow
from jarvis.finance.personal_cfo import PersonalCFO, money
from jarvis.finance.personal_profile import business_date


class PersonalAnalyst:
    """Source-bound explanations under the common budget and controlled fallback."""

    def __init__(self, cfo: PersonalCFO, directory: Path, *, monthly_limit: str = "250"):
        self.cfo, self.directory = cfo, directory
        directory.mkdir(parents=True, exist_ok=True)
        self.monthly = money(monthly_limit)
        if not 0 < self.monthly <= 250:
            raise ValueError("Personal monthly LLM limit must be within 0..250 RUB")
        self.journal = BudgetStore(directory / "personal-llm.sqlite", currency="RUB")

    def evidence(self) -> dict[str, Any]:
        data = self.cfo.dashboard()
        today = business_date(datetime.now(UTC))
        rows = self.cfo.economic_rows(self.cfo.db.transactions_between(today-timedelta(days=89),today))
        with self.cfo.db.connect() as conn:
            confirmed = {r[0] for r in conn.execute("SELECT DISTINCT c.transaction_id FROM category_corrections c JOIN transactions t ON t.id=c.transaction_id WHERE c.source='manual' AND c.new_category=t.category AND version=(SELECT MAX(c2.version) FROM category_corrections c2 WHERE c2.transaction_id=c.transaction_id)")}
        known = [r for r in rows if r["id"] in confirmed]
        flow = cashflow(known)
        unknown = [r for r in rows if r["id"] not in confirmed and r["currency"] == "RUB" and r["category"] not in NON_ECONOMIC_CATEGORIES and money(r["amount"]) < 0]
        count = len((data["bank"] or {}).get("accounts", []))
        today = business_date(datetime.now(UTC))
        identity = hashlib.sha256(json.dumps({"rows":rows,"confirmed":sorted(confirmed),"bank":data["bank"],"period":[str(today-timedelta(days=89)),str(today)]}, sort_keys=True, default=str).encode()).hexdigest()
        return {"source_sha256": identity, "snapshot_version": identity, "binding": identity,
            "fields": {"period": [(today - timedelta(days=89)).isoformat(), today.isoformat()],
            "known_expenses": str(flow["expenses"]), "currency": "RUB",
            "unknown_debits": str(sum((abs(money(r["amount"])) for r in unknown), Decimal(0))),
            "coverage_complete": False, "covered_accounts": 0, "known_accounts": count,
            "unresolved_categories": len(unknown), "known_by_category": {k: str(v) for k, v in flow["by_category"].items()}}}

    async def explain(self, *, provider: OpenAICompatProvider | None = None,
                      pricing_path: Path | None = None) -> dict[str, Any]:
        """Generate validated fact references; render all amounts locally with Decimal."""
        from jarvis.core.llm.intelligence import Intelligence
        from jarvis.core.llm.orchestrator import complete_intelligence
        runtime: Intelligence | None
        if provider is not None:
            if not isinstance(provider.transport, httpx.MockTransport):
                return {'status': 'unavailable', 'code': 'injected_live_provider_refused', 'paid_cost_rub': '0'}
            self.journal.initialize()
            self.journal.enable_global()
            if pricing_path is None:
                raise ValueError("Injected provider requires explicit price evidence")
            runtime = Intelligence(self.journal, pricing_path)
        else:
            from jarvis.core.llm.governance import canonical_journal
            self.journal = BudgetStore(canonical_journal(), currency='RUB')
            runtime = None
        evidence = self.evidence()

        def validate(value: dict[str, Any]) -> dict[str, Any]:
            result = checked_reasoning(ReasoningCandidate.model_validate(value), evidence)
            return {"text": render_reasoning(result).replace('Qwen ·', 'AI ·', 1)}

        options: dict[str, Any] = {"max_output": 400, "fallback": provider is None}
        if provider is not None:
            options['provider_factory'] = lambda: provider
        result = await complete_intelligence('analysis', reasoning_prompt(evidence), validate, runtime=runtime, **options)
        if result.get('result'):
            result['text'] = result['result']['text']
        return result
