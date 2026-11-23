"""Explicit owner activation or isolated synthetic replay; never starts a daemon."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvis.core.llm.budget_store import BudgetStore
from jarvis.core.llm.orchestrator import complete_public_research_plan
from jarvis.core.llm.providers.openai_compat import OpenAICompatProvider
from jarvis.finance.scheduler.agent import AgentTask
from jarvis.finance.scheduler.research_demo import research_runtime
from jarvis.finance.scheduler.research_policy import ResearchPolicies, ResearchPolicy


async def execute(folder: Path, policy_id: str, *, owner_live: bool = False, question: str | None = None,
                  planner: bool = False, budget: Path | None = None, prices: Path | None = None) -> dict[str, Any]:
    core = research_runtime(folder, owner_public_http=owner_live)
    policies = ResearchPolicies(core.journal)
    policy = policies.check(policy_id, synthetic=not owner_live)
    topic = question or policy.topics[0]
    if topic not in policy.topics:
        raise PermissionError("Topic not approved")
    if core.journal.get_setting("active_research_policy") != policy_id:
        raise PermissionError("Active policy identity mismatch")
    assert core.public_sources is not None
    core.public_sources.question = topic
    core.research_planner = None
    if planner:
        if not owner_live or budget is None or prices is None or not budget.is_file() or not prices.is_file():
            raise PermissionError("Explicit owner live planner with existing budget/prices required")
        async def plan(identity: str) -> dict[str, Any]:
            return await complete_public_research_plan(journal_path=budget, pricing_path=prices, event_identity=identity,
                transport=None, policies=policies, policy_id=policy_id, task_id=ident, question=topic)
        core.research_planner = plan
    core.journal.set_setting("research_selected_topic", topic)
    core.journal.set_setting("research_planner_enabled", planner)
    task = AgentTask(intent="public_research", origin="user", event_key="policy:" + policy_id,
        input_revision=core.revision(), research_policy_id=policy_id, source_scope=("public_research",),
        maximum_steps=5 if planner else 4, run_budget_rub="0.03" if planner else "0",
        timeout_seconds=20, deadline=policy.expires_at)
    ident = core.submit(task)
    if planner and core.inspect(ident)["state"] == "QUEUED":
        invocation = policies.planner_identity(policy_id, ident, event_identity=str(ident) + task.input_revision,
            model="qwen3.7-flash", synthetic=False, question=topic)
        assert budget is not None
        store = BudgetStore(budget, currency="RUB")
        scope = hashlib.sha256(("public-research-grant-v2:" + policy_id + ":public_research:qwen3.7-flash").encode()).hexdigest()
        if store.request_status(invocation["fingerprint"]) is not None or (invocation["version"].endswith("v2") and store.bound_request(scope) is not None):
            return core.transition(ident, "WAITING_FOR_DATA", {"reason": "Recorded invocation; refused before key/provider HTTP",
                "observation": {"status": "unavailable", "request_identity": invocation["fingerprint"], "identity_version": invocation["version"]}})
        from jarvis.knowledge.public_sources import QUESTION
        if topic == QUESTION:
            raise PermissionError("Known duration workflow is deterministic; omit --planner")
        policies.consume(policy_id, ident, "task", "claim", synthetic=False, question=topic)
        policies.consume(policy_id, ident, "key_check", "aitunnel_key_status", synthetic=False, question=topic)
        await OpenAICompatProvider().owner_key_status(owner_action=True)
    return await core.run(ident)


def main() -> None:
    parser = argparse.ArgumentParser(description="Purpose-bound public research, paid search OFF")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--grant-policy", type=Path)
    parser.add_argument("--prepare-policy", type=Path, help="Write an unsigned proposal only, no grant or network")
    parser.add_argument("--owner-authorize", action="store_true")
    parser.add_argument("--policy-id")
    parser.add_argument("--revoke-policy")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--planner", action="store_true")
    parser.add_argument("--question")
    parser.add_argument("--budget-journal", type=Path)
    parser.add_argument("--prices", type=Path)
    args = parser.parse_args()
    if args.prepare_policy:
        from jarvis.core.config import settings
        from jarvis.knowledge.public_sources import QUESTION
        proposal = ResearchPolicy(mode="live", paid_llm=args.planner,
            topics=(args.question or QUESTION,),
            urls=("https://www.moex.com/ru/articles/chto-takoe-dyuratsiya-obligatsij-prostymi-slovami", "https://bondguide.moex.com/terms/31"),
            domains=("www.moex.com", "bondguide.moex.com"), expires_at=datetime.now(UTC)+timedelta(hours=1),
            daily_budget_rub=Decimal(str(settings.daily_budget_rub)), monthly_budget_rub=Decimal(str(settings.monthly_budget_rub)))
        with args.prepare_policy.open("x", encoding="utf-8") as target:
            target.write(proposal.model_dump_json(indent=2))
        print(json.dumps({"status": "proposal_only", "approval_issued": False})); return
    # Construct only a synthetic staging workspace until genuine policy validation.
    if args.grant_policy:
        policy = ResearchPolicy.model_validate_json(args.grant_policy.read_text(encoding="utf-8-sig"))
        from jarvis.finance.scheduler.workflow_demo import demo_runtime
        core = demo_runtime(args.workspace)
        policies = ResearchPolicies(core.journal)
        ident = policies.grant(policy, owner_action=args.owner_authorize)
        core.journal.set_setting("active_research_policy", ident)
    else:
        from jarvis.finance.scheduler.workflow_demo import demo_runtime
        core = demo_runtime(args.workspace)
        policies = ResearchPolicies(core.journal)
        ident = args.policy_id or core.journal.get_setting("active_research_policy")
    if args.revoke_policy:
        policies.revoke(args.revoke_policy)
        print(json.dumps({"status": "revoked"})); return
    if not ident:
        parser.error("Explicit existing policy required")
    saved = core.journal.get_setting("research_policy:" + ident)
    if args.run:
        result = asyncio.run(execute(args.workspace, ident, owner_live=saved["policy"]["mode"] == "live",
            question=args.question, planner=args.planner, budget=args.budget_journal, prices=args.prices))
        print(json.dumps(result, ensure_ascii=False))
        if result["state"] != "COMPLETED":
            raise SystemExit(2)
    else:
        print(json.dumps({"status": "granted", "policy_id": ident, "issued_at": datetime.now(UTC).isoformat()}))


if __name__ == "__main__":
    from jarvis.core.llm.budget_store import (
        BudgetReservationDenied,
        BudgetStoreUnavailable,
    )
    from jarvis.core.llm.errors import LLMError
    try:
        main()
    except (ValueError, PermissionError, OSError, LLMError, BudgetReservationDenied, BudgetStoreUnavailable):
        print(json.dumps({"status": "blocked", "reason": "Permission, input, budget or provider refused; no automatic retry"}))
        raise SystemExit(2) from None
