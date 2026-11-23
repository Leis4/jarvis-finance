"""Purpose-bound mock planner using the existing provider and RUB BudgetStore."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from jarvis.core.config import settings
from jarvis.core.llm.budget_store import (
    BudgetReservationDenied,
    BudgetStore,
    UsageReceipt,
)
from jarvis.core.llm.errors import (
    LLMError,
    LLMPricingUnavailable,
    LLMResponseUnavailable,
    LLMUnavailable,
)
from jarvis.core.llm.planner_diagnostics import sanitized
from jarvis.core.llm.pricing import aitunnel_price_evidence
from jarvis.core.llm.providers.openai_compat import (
    OpenAICompatProvider,
    require_owner_key_check,
)
from jarvis.finance.scheduler.research_policy import ResearchPolicies
from jarvis.knowledge.public_sources import QUESTION

TOOLS = ("public_candidates", "read_public_sources", "explain_public_sources", "prepare_report")


def dependency_schema(schema: dict[str, Any]) -> None:
    """Expose the same dependency contract used by the local validator."""
    schema["oneOf"] = [{"properties": {"decision": {"const": decision},
        "tools": {"const": list(ResearchPlan.required_tools(decision))}}}
        for decision in schema["properties"]["decision"]["enum"]]


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=dependency_schema)
    purpose: Literal["public_research"]
    decision: Literal["execute", "insufficient_data"]
    tools: tuple[Literal["public_candidates", "read_public_sources", "explain_public_sources", "prepare_report"], ...]

    @model_validator(mode="before")
    @classmethod
    def typed_failures(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for name in ("purpose", "decision"):
                if name in value and not isinstance(value[name], str):
                    raise PydanticCustomError("incorrect_json_types", "JSON type refused")
            items = value.get("tools")
            if isinstance(items, (list, tuple)):
                if any(isinstance(item, dict) and ("arguments" in item or "args" in item) for item in items):
                    raise PydanticCustomError("extra_tool_arguments", "Tool arguments refused")
                if any(not isinstance(item, str) for item in items):
                    raise PydanticCustomError("incorrect_json_types", "JSON type refused")
        return value

    @staticmethod
    def required_tools(decision: str) -> tuple[str, ...]:
        return TOOLS if decision == "execute" else ()

    @model_validator(mode="after")
    def dependencies(self) -> ResearchPlan:
        if self.tools != self.required_tools(self.decision):
            cause = ("invalid_dependencies" if self.decision == "insufficient_data" else
                "partial_plan" if len(self.tools) < len(TOOLS) else
                "wrong_step_order" if sorted(self.tools) == sorted(TOOLS) else "invalid_dependencies")
            raise PydanticCustomError(cause, "Closed dependency contract refused")
        return self


def validation_diagnostics(exc: ValidationError) -> dict[str, object]:
    """Project error codes only; never return inputs, locations or messages."""
    codes = {error["type"] for error in exc.errors(include_input=False, include_context=False, include_url=False)}
    categories = set()
    for code in codes:
        categories.add({"missing": "missing_required_fields", "extra_forbidden": "unexpected_fields",
            "model_type": "incorrect_json_types", "tuple_type": "incorrect_json_types"}.get(code,
            code if code in {"invalid_dependencies", "partial_plan", "wrong_step_order", "extra_tool_arguments", "incorrect_json_types"} else
            "invalid_literal" if code == "literal_error" else "incorrect_json_types"))
    for error in exc.errors(include_input=False, include_context=False, include_url=False):
        if error["type"] == "literal_error" and error["loc"] and error["loc"][0] == "tools":
            categories.add("wrong_step_names")
        if error["type"] == "extra_forbidden" and error["loc"] and error["loc"][0] in {"args", "arguments"}:
            categories.add("extra_tool_arguments")
    return {"validation_failures": sorted(categories)}


def planner_prompt(question: str, approved_sources: tuple[str, ...] = ()) -> str:
    """Generate bounded model instructions/examples from the closed schema."""
    example = ResearchPlan.model_validate({"purpose": "public_research", "decision": "execute", "tools": TOOLS})
    if any(urlsplit(url).query or urlsplit(url).fragment or urlsplit(url).username for url in approved_sources):
        raise PermissionError("Public model catalog refuses credentials/query/fragment")
    return ("Return one JSON object, no other text. Select execute only if the approved source catalog can plausibly "
        "answer the PUBLIC question; otherwise insufficient_data. URLs are scope hints, not evidence. "
        "Use exactly the schema fields, literals and conditional dependencies; no arguments or extra tools.\n"
        + json.dumps({"question": question, "approved_sources": approved_sources,
            "schema": ResearchPlan.model_json_schema(), "example": example.model_dump(mode="json")},
            ensure_ascii=False, separators=(",", ":")))


async def mock_plan(*, journal_path: Path, pricing_path: Path, event_identity: str,
                    transport: httpx.MockTransport | None, policies: ResearchPolicies | None = None,
                    policy_id: str | None = None, task_id: int = 0, question: str = QUESTION) -> dict[str, Any]:
    """One bounded planner attempt; mock authority never admits a live transport."""
    synthetic = isinstance(transport, httpx.MockTransport)
    if not synthetic:
        raise PermissionError("Paid research planner disabled until global budget integration")
    if settings.aitunnel_model != "qwen3.7-flash" or (synthetic and settings.llm_mode != "mock"):
        raise PermissionError("Pinned planner mode required")
    policy = None
    if policies is not None and policy_id is not None:
        policy = policies.check(policy_id, synthetic=synthetic)
        if question not in policy.topics or not policy.paid_llm:
            raise PermissionError("Planner purpose permission refused")
    if not synthetic:
        if (transport is not None or policy is None or settings.llm_mode != "cloud_opt_in"
                or os.environ.get("CODEX_THREAD_ID") or os.environ.get("PYTEST_CURRENT_TEST")):
            raise PermissionError("Separate owner live planner approval required")
        if question == QUESTION:
            raise PermissionError("Known duration workflow is deterministic; no paid planner")
        try:
            require_owner_key_check(settings.llm_api_key, settings.aitunnel_model, now=datetime.now(UTC))
        except LLMError:
            raise LLMUnavailable("Planner key preflight refused", diagnostics={"stage": "key_preflight", "request_evidence": "not_sent", "billing": "not_attempted", "error_type": "key"}) from None
    elif policy is None and question != QUESTION:
        raise PermissionError("Custom topic needs a bound policy")
    if not isinstance(event_identity, str) or not 1 <= len(event_identity) <= 200:
        raise ValueError("Bounded planner event required")
    try:
        price = aitunnel_price_evidence(pricing_path, settings.aitunnel_model, now=datetime.now(UTC))
    except LLMPricingUnavailable:
        raise LLMUnavailable("Planner pricing preflight refused", diagnostics={"stage": "pricing_preflight", "request_evidence": "not_sent", "billing": "not_attempted", "error_type": "price"}) from None
    ceiling = min(Decimal(str(settings.request_ceiling_rub)), policy.request_budget_rub if policy else Decimal("0.03"))
    if price.reserved_rub > ceiling:
        raise ValueError("Research request budget refused")
    prompt = planner_prompt(question, policy.urls if policy else ())
    if len(prompt.encode()) + 256 > price.input_cap:
        raise ValueError("Planner prompt budget refused")
    ident = hashlib.sha256(("public-research-plan-v1:" + event_identity + price.model).encode()).hexdigest()
    identity_version = "public-research-plan-v1"
    scope_identity = None
    if policies is not None and policy_id is not None:
        binding = policies.planner_identity(policy_id, task_id, event_identity=event_identity,
            model=price.model, synthetic=synthetic, question=question)
        ident, identity_version = binding["fingerprint"], binding["version"]
        if identity_version == "public-research-plan-v2":
            scope_identity = hashlib.sha256(("public-research-grant-v2:" + str(policy_id) + ":public_research:" + price.model).encode()).hexdigest()
    journal = BudgetStore(journal_path, currency="RUB", simulation=synthetic)
    if journal.request_status(ident) is not None:
        return {"status": "unavailable", "reason": "Recorded planner attempt; no automatic resend" if identity_version.endswith("v2") else "Legacy unscoped attempt/collision; no automatic resend",
                "request_identity": ident, "identity_version": identity_version}
    if scope_identity is not None and journal.bound_request(scope_identity) is not None:
        return {"status": "unavailable", "reason": "Owner grant already bound to another invocation; no automatic resend",
                "request_identity": ident, "identity_version": identity_version}
    journal.reserve(ident, price.reserved_rub,
        min(Decimal(str(settings.daily_budget_rub)), policy.daily_budget_rub if policy else Decimal(str(settings.daily_budget_rub))),
        min(Decimal(str(settings.monthly_budget_rub)), policy.monthly_budget_rub if policy else Decimal(str(settings.monthly_budget_rub))),
        price.evidence_sha256, now=datetime.now(UTC), scope_identity=scope_identity)
    diagnostic: dict[str, object] = {"stage": "provider", "request_evidence": "transport_attempted", "billing": "unknown"}
    try:
        if policies is not None and policy_id is not None:
            policies.consume(policy_id, task_id, "model", price.model, synthetic=synthetic, question=question)
    except BaseException:
        journal.release(ident)
        raise
    journal.observe("request_binding", event_id=ident, request_id=ident, model=price.model, price_sha256=price.evidence_sha256)
    journal.mark_attempted(ident, model=price.model)
    provider = OpenAICompatProvider(api_key="synthetic-planner-only" if synthetic else settings.llm_api_key,
        base_url="https://api.aitunnel.ru/v1", transport=transport)
    try:
        failure = False
        try:
            content, tokens_in, tokens_out = await provider.complete(price.model, prompt, price.output_cap,
                json_mode=True, require_rub_usage=True, reasoning_effort="none")
            diagnostic.update(sanitized(provider.response_diagnostics))
        except LLMResponseUnavailable as exc:
            if provider.rub_usage is None or exc.diagnostics is None:
                raise
            failure, content = True, ""
            diagnostic.update(sanitized(exc.diagnostics))
            tokens_in, tokens_out = int(str(exc.diagnostics["prompt_tokens"])), int(str(exc.diagnostics["completion_tokens"]))
        if provider.rub_usage is None or tokens_in > price.input_cap:
            raise ValueError("Planner usage unavailable")
        actual = Decimal(str(provider.rub_usage["cost_rub"]))
        diagnostic.update(cost_rub=str(actual), request_evidence="http_response")
        if not failure:
            diagnostic.update(model=price.model, prompt_tokens=tokens_in, completion_tokens=tokens_out)
        journal.settle(ident, actual, now=datetime.now(UTC), receipt=UsageReceipt(price.model, tokens_in, tokens_out,
            hashlib.sha256(prompt.encode()).hexdigest(), hashlib.sha256(content.encode()).hexdigest()),
            reasoning_tokens=cast(int | None, provider.rub_usage.get("reasoning_tokens")),
            cached_tokens=cast(int | None, provider.rub_usage.get("cached_tokens")))
    except BaseException as exc:
        try:
            journal.mark_uncertain(ident)
        except BudgetReservationDenied:
            pass
        if isinstance(exc, LLMError):
            diagnostic.update(sanitized(getattr(exc, "diagnostics", None)))
            diagnostic["request_evidence"] = "http_response" if "http_status" in diagnostic else "transport_attempted"
            raise LLMUnavailable("Planner provider unavailable; no automatic resend", diagnostics=sanitized(diagnostic)) from None
        diagnostic.update(stage="settlement", error_type="settlement")
        exc.__dict__["planner_diagnostics"] = sanitized(diagnostic)
        raise
    diagnostic["billing"] = "settled"
    base = {"simulation": synthetic, "paid_cost_rub": "0" if synthetic else str(actual), "simulated_cost_rub": str(actual) if synthetic else None,
            "reserved_rub": str(price.reserved_rub), "request_identity": ident, "identity_version": identity_version, "model": price.model}
    if failure or actual > price.reserved_rub:
        return {**base, "status": "unavailable", "reason": "Planner response/billing refused; known debit retained", "diagnostics": sanitized(diagnostic)}
    try:
        decoded = json.loads(content)
    except ValueError:
        diagnostic.update(stage="completion_json", error_type="invalid_json")
        return {**base, "status": "unavailable", "reason": "Planner JSON refused; known debit retained", "diagnostics": sanitized(diagnostic)}
    try:
        plan = ResearchPlan.model_validate(decoded)
    except ValidationError as exc:
        diagnostic.update(stage="plan_schema", error_type="invalid_plan")
        diagnostic.update(validation_diagnostics(exc))
        return {**base, "status": "unavailable", "reason": "Planner schema/dependency refused; known debit retained", "diagnostics": sanitized(diagnostic)}
    return {**base, "status": "available" if plan.decision == "execute" else "insufficient_data", "plan": plan.model_dump(mode="json"), "diagnostics": sanitized(diagnostic)}
