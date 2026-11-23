"""Bounded offline HTTP-error projection; never emit server text or headers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CATEGORIES = frozenset({"invalid_request", "unsupported_response_format", "unsupported_reasoning",
    "invalid_model", "context_limit", "provider_content_rejection", "unknown_http400"})


def classify(status: int, body: object) -> dict[str, object]:
    """Classify explicit error signals only; arbitrary text stays unrecorded."""
    result: dict[str, object] = {"http_error_category": "unknown_http400" if status == 400 else "unclassified_http",
        "error_object_present": False, "error_message_present": False, "error_metadata_present": False,
        "classification_basis": "insufficient_evidence"}
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return result
    message, metadata, code = error.get("message"), error.get("metadata"), error.get("code")
    result.update(error_object_present=True, error_message_present=isinstance(message, str),
                  error_metadata_present=isinstance(metadata, dict))
    if type(code) is int and 100 <= code <= 599:
        result["server_error_status"] = code
        if code != status:
            return result
    if status != 400:
        return result
    codes = {"invalid_request": "invalid_request", "invalid_request_error": "invalid_request",
        "unsupported_response_format": "unsupported_response_format", "unsupported_reasoning": "unsupported_reasoning",
        "invalid_model": "invalid_model", "model_not_found": "invalid_model",
        "context_length_exceeded": "context_limit", "context_limit": "context_limit",
        "content_policy_violation": "provider_content_rejection", "pii_detected": "provider_content_rejection"}
    if isinstance(code, str) and code in codes:
        result.update(http_error_category=codes[code], classification_basis="explicit_code")
        return result
    # Exact, bounded messages: no substring guessing from arbitrary echoed input.
    messages = {"unsupported parameter: response_format": "unsupported_response_format",
        "unsupported response_format": "unsupported_response_format", "response_format is not supported": "unsupported_response_format",
        "unsupported parameter: reasoning": "unsupported_reasoning", "reasoning is not supported": "unsupported_reasoning",
        "invalid json": "invalid_request", "invalid request": "invalid_request",
        "unknown model": "invalid_model", "model not found": "invalid_model",
        "context length exceeded": "context_limit"}
    if isinstance(message, str) and len(message) <= 128:
        normalized = " ".join(message.casefold().split()).rstrip(".")
        if normalized == "'messages' must contain the word 'json' in some form, to use 'response_format' of type 'json_object'":
            result.update(http_error_category="invalid_request", classification_basis="explicit_message",
                          request_constraint="json_instruction_required")
            return result
        if normalized in messages:
            result.update(http_error_category=messages[normalized], classification_basis="explicit_message")
            return result
    if (isinstance(metadata, dict) and isinstance(metadata.get("flagged_input"), str)
            and isinstance(metadata.get("provider_name"), str) and isinstance(metadata.get("model_slug"), str)
            and isinstance(metadata.get("reasons"), list) and metadata["reasons"]
            and all(isinstance(item, str) for item in metadata["reasons"])):
        result.update(http_error_category="provider_content_rejection", classification_basis="moderation_metadata")
    return result


def parse_error(status: int, raw: bytes) -> dict[str, object]:
    """Reject oversize/malformed input without copying it into diagnostics."""
    if not isinstance(raw, bytes) or len(raw) > 32_768:
        return classify(status, None)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        body = None
    return classify(status, body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline redacted error projection; no HTTP or billing changes")
    parser.add_argument("--error-file", type=Path, required=True)
    parser.add_argument("--http-status", type=int, choices=range(400, 600), required=True)
    parser.add_argument("--output", type=Path, required=True, help="NEW sanitized JSON only")
    args = parser.parse_args()
    try:
        if not args.error_file.is_file() or args.error_file.is_symlink() or args.error_file.stat().st_size > 32_768:
            raise ValueError("Bounded private error file required")
        with args.error_file.open("rb") as source:
            diagnostic = parse_error(args.http_status, source.read(32_769))
        # Exclusive creation; never overwrite an original or an existing report.
        with args.output.open("x", encoding="utf-8") as target:
            target.write(json.dumps(diagnostic, ensure_ascii=False, indent=2))
        print(json.dumps(diagnostic, ensure_ascii=False))
    except (ValueError, OSError):
        print(json.dumps({"status": "unavailable", "reason": "Offline evidence/output refused; no HTTP or journal changes"}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
