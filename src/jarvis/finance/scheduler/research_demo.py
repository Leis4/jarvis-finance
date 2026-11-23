"""Synthetic research through the real staging agent; no live HTTP or paid calls."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from jarvis.finance.scheduler.agent import AgentRuntime, AgentTask
from jarvis.finance.scheduler.workflow_demo import demo_runtime
from jarvis.knowledge.journal import IndexJournal
from jarvis.knowledge.public_sources import PublicSources


def research_runtime(folder: Path, *, owner_public_http: bool = False) -> AgentRuntime:
    if owner_public_http and (os.environ.get("CODEX_THREAD_ID") or os.environ.get("PYTEST_CURRENT_TEST")):
        raise PermissionError("Separate owner action outside Codex/tests required")
    core = demo_runtime(folder)
    knowledge = folder / "knowledge"
    knowledge.mkdir(exist_ok=True)

    def response(request: httpx.Request) -> httpx.Response:
        text = ("<h1>SYNTHETIC duration fixture</h1><p>Дюрация показывает чувствительность цены облигации к изменению процентных ставок.</p>"
                if request.url.path == "/duration" else
                "<h1>SYNTHETIC limitation fixture</h1><p>Дюрация оценивает реакцию цены на небольшие изменения ставок; для больших изменений нужна выпуклость.</p>")
        return httpx.Response(200, text=text, headers={"content-type": "text/html"})

    sources = (PublicSources(IndexJournal(knowledge / "lineage.sqlite"),
        ("https://www.moex.com/ru/articles/chto-takoe-dyuratsiya-obligatsij-prostymi-slovami", "https://bondguide.moex.com/terms/31"),
        frozenset({"www.moex.com", "bondguide.moex.com"})) if owner_public_http else
        PublicSources(IndexJournal(knowledge / "lineage.sqlite"),
            ("https://research.example/duration", "https://research.example/limitations"), frozenset({"research.example"}),
            transport=httpx.MockTransport(response)))
    active = core.journal.get_setting("active_research_policy")
    if active:
        from jarvis.finance.scheduler.research_policy import ResearchPolicy
        saved = core.journal.get_setting("research_policy:" + active)
        policy = ResearchPolicy.model_validate(saved["policy"])
        if (policy.mode == "live") != owner_public_http:
            raise PermissionError("Policy transport mismatch")
        question = core.journal.get_setting("research_selected_topic", policy.topics[0])
        if question not in policy.topics:
            raise PermissionError("Topic not approved")
        sources = PublicSources(sources.journal, policy.urls, frozenset(policy.domains), question=question, search_endpoint=policy.search_endpoint, discovery_scope=active,
            transport=None if owner_public_http else httpx.MockTransport(response))
    async def inactive_planner(identity: str) -> dict[str, Any]:
        return {"status": "blocked", "reason": "Separate owner planner action required"}
    return AgentRuntime(core.journal, core.store, public_sources=sources,
        research_planner=inactive_planner if active and core.journal.get_setting("research_planner_enabled", False) else None)


async def rehearsal(folder: Path, *, owner_public_http: bool = False) -> dict[str, Any]:
    core = research_runtime(folder, owner_public_http=owner_public_http)
    # Resume the same recorded deadline rather than silently authorizing a new request.
    with core.journal.connect() as conn:
        row = conn.execute("SELECT id FROM scheduled_tasks WHERE task_type='agent_core' AND json_extract(params,'$.intent')='public_research' ORDER BY id DESC LIMIT 1").fetchone()
    if row is not None:
        return await core.run(row[0])
    task = AgentTask(intent="public_research", origin="research", event_key="synthetic-duration-v1",
        input_revision=core.revision(), source_scope=("public_research",), maximum_steps=4,
        deadline=datetime.now(UTC) + timedelta(hours=1), timeout_seconds=15)
    return await core.run(core.submit(task))


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic public research, no external requests")
    parser.add_argument("--demo-workspace", type=Path, required=True)
    parser.add_argument("--approve-public-research", action="store_true", help="Owner-only free public HTTPS; no model/search billing")
    args = parser.parse_args()
    result = asyncio.run(rehearsal(args.demo_workspace, owner_public_http=args.approve_public_research))
    print(json.dumps(result, ensure_ascii=False))
    if result["state"] != "COMPLETED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
