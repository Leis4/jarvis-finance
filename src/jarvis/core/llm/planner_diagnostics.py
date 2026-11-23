"""Closed, content-free research planner diagnostic contract."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def sanitized(value: object) -> dict[str, object]:
    """Reject arbitrary text, including exception messages and model outputs."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    failures = value.get("validation_failures")
    allowed_failures = {"missing_required_fields", "unexpected_fields", "incorrect_json_types", "invalid_literal",
        "invalid_dependencies", "partial_plan", "wrong_step_order", "wrong_step_names", "extra_tool_arguments"}
    if isinstance(failures, list):
        result["validation_failures"] = sorted({item for item in failures if isinstance(item, str) and item in allowed_failures})
    enums = {
        "stage": {"key_preflight", "pricing_preflight", "provider", "completion_json", "plan_schema", "settlement", "sources"},
        "request_evidence": {"not_sent", "transport_attempted", "http_response", "historical_unknown"},
        "billing": {"not_attempted", "unknown", "settled"},
        "error_type": {"transport", "http", "response", "usage", "tool_calls", "length", "content_type", "empty_content", "finish_reason", "invalid_json", "invalid_plan", "key", "price", "settlement", "source", "unknown"},
        "finish_reason": {"stop", "length", "tool_calls", "content_filter", "unknown"},
        "content_type": {"string", "null", "unexpected", "unknown"},
        "http_error_category": {"invalid_request", "unsupported_response_format", "unsupported_reasoning", "invalid_model", "context_limit", "provider_content_rejection", "unknown_http400", "unclassified_http"},
        "classification_basis": {"insufficient_evidence", "explicit_code", "explicit_message", "moderation_metadata"},
        "request_constraint": {"json_instruction_required"},
    }
    for key, choices in enums.items():
        if isinstance(value.get(key), str) and value[key] in choices:
            result[key] = value[key]
    for key in ("http_status", "server_error_status", "content_length", "completion_tokens", "prompt_tokens", "reasoning_tokens"):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 10_000_000:
            result[key] = number
    if type(value.get("reasoning_present")) is bool:
        result["reasoning_present"] = value["reasoning_present"]
    for key in ("error_object_present", "error_message_present", "error_metadata_present"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if value.get("model") == "qwen3.7-flash":
        result["model"] = value["model"]
    try:
        cost = Decimal(str(value.get("cost_rub")))
        if cost.is_finite() and 0 <= cost <= 1_000_000:
            result["cost_rub"] = str(cost)
    except InvalidOperation:
        pass
    return result
