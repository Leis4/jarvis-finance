import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from jarvis.core.config import settings
from jarvis.core.llm.errors import (
    LLMBudgetExceeded,
    LLMResponseUnavailable,
    LLMUnavailable,
    LLMUsageUnavailable,
)
from jarvis.core.llm.providers.http_error_diagnostics import parse_error

_OWNER_KEY_CHECKS: dict[str, tuple[datetime, str, Decimal, datetime | None]] = {}


def chat_fingerprint_payload(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
    """Bind the entire bounded native tool protocol to its paid dispatch permit."""
    if not 1 <= len(messages) <= 32 or len(tools) > 8:
        raise ValueError('Bounded conversation/tools required')
    if any(m.get('role') not in {'system','user','assistant','tool'} for m in messages):
        raise ValueError('Unknown conversation role')
    value=json.dumps({'messages':messages,'tools':tools},ensure_ascii=False,sort_keys=True,separators=(',',':'))
    if len(value.encode())>23500:
        raise ValueError('Conversation exceeds input bound')
    return value


def require_owner_key_check(api_key: str, model: str, *, now: datetime) -> None:
    """Require fresh live key settings in this process, bound to current key/model."""
    checked = _OWNER_KEY_CHECKS.get(hashlib.sha256(api_key.encode()).hexdigest())
    if (checked is None or checked[1] != model or checked[0] > now or now - checked[0] > timedelta(minutes=5)
            or (checked[3] is not None and checked[3] <= now)
            or checked[2] <= 0):
        raise LLMUnavailable("Fresh owner key settings check required; configuration flag is not evidence")


class OpenAICompatProvider:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key or settings.llm_api_key
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.timeout_s = timeout_s or settings.llm_timeout_s
        self.rub_usage: dict[str, object] | None = None
        self.response_diagnostics: dict[str, object] = {}
        self.provider_request_id: str | None = None
        self.response_content: str | None = None
        self.response_message: dict[str, Any] | None = None
        self.stage_events: list[dict[str, Any]] = []
        self.last_stage: str | None = None
        self.transport = transport

    def _mark(self, stage: str, **fields: Any) -> None:
        """Record a safe processing stage; never prompts, bodies or headers."""
        self.last_stage = stage
        self.stage_events.append({'stage': stage, 'at': time.monotonic(), **fields})

    async def owner_key_status(self, *, owner_action: bool = False, model: str = "qwen3.7-flash",
                               now: datetime | None = None, global_policy: bool = False) -> dict[str, object]:
        """Explicit owner-only authenticated status; mocks never mint live readiness."""
        current = now or datetime.now(UTC)
        mock = isinstance(self.transport, httpx.MockTransport)
        if (not owner_action or current.tzinfo is None or not self.api_key
                or model not in {"qwen3.7-flash", "qwen3.7-plus", "qwen3.7-max", "gemini-2.5-flash-lite", "gpt-6-luna", "deepseek-v4-flash-0731"}
                or self.base_url != "https://api.aitunnel.ru/v1"
                or (not mock and not global_policy and (os.environ.get("CODEX_THREAD_ID") or os.environ.get("PYTEST_CURRENT_TEST")
                                 or settings.llm_mode != "cloud_opt_in"))):
            raise LLMUnavailable("Separate owner key status action required")
        fingerprint = hashlib.sha256(self.api_key.encode()).hexdigest()
        # A failed recheck revokes the old successful result.
        _OWNER_KEY_CHECKS.pop(fingerprint, None)
        try:
            async with httpx.AsyncClient(timeout=min(self.timeout_s, 15), transport=self.transport, trust_env=False) as client:
                response = await client.get(self.base_url + "/aitunnel/key", headers={"Authorization": "Bearer " + self.api_key})
            if response.status_code != 200 or len(response.content) > 32_768:
                raise ValueError
            data = response.json()
            budget = data["budget"]
            initial, remaining = Decimal(str(budget["initial"])), Decimal(str(budget["remaining"]))
            allowed = data["allowed_models"]
            if (not initial.is_finite() or not remaining.is_finite() or initial <= 0 or not 0 <= remaining <= initial
                    or (allowed is not None and (not isinstance(allowed, list) or not all(isinstance(m, str) for m in allowed)))):
                raise ValueError
            expires = datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
            if expires is not None and expires.tzinfo is None:
                raise ValueError
            ready = (budget["reset_interval"] == "monthly"
                and remaining >= Decimal(str(settings.request_ceiling_rub))
                and (allowed is None or model in allowed) and (expires is None or expires > current))
            result: dict[str, object] = {"status": "ready" if ready else "blocked", "mock": mock,
                "checked_at": current.isoformat(), "budget_initial_rub": str(initial), "budget_remaining_rub": str(remaining),
                "reset_interval": "monthly" if budget["reset_interval"] == "monthly" else "nonmonthly",
                "model_permitted": allowed is None or model in allowed, "expires_at": expires.isoformat() if expires else None,
                "model": model, "live_readiness": ready and not mock,
                "provider_ceiling_is_application_limit": False,
                "application_daily_limit_rub": str(settings.daily_budget_rub),
                "application_monthly_limit_rub": str(settings.monthly_budget_rub),
                "atomic_local_reservation_required": True}
            if ready and not mock:
                _OWNER_KEY_CHECKS[fingerprint] = (current, model, initial, expires)
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
            raise LLMUnavailable("Owner key settings unavailable or invalid") from None

    async def complete(
        self,
        model: str,
        prompt: str,
        max_tokens: int,
        json_mode: bool = False,
        require_rub_usage: bool = False,
        reasoning_effort: str = "low",
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[str, int, int]:
        self.rub_usage = None
        self.response_diagnostics = {}
        self.provider_request_id = None
        self.response_content = None
        self.response_message = None
        self.stage_events = []
        self.last_stage = None
        self._mark('preflight_started')
        if messages is not None and prompt != chat_fingerprint_payload(messages, tools or []):
            raise LLMUnavailable('Conversation differs from reserved prompt')
        from jarvis.core.llm.governance import consume_dispatch
        governed = False
        if not isinstance(self.transport, httpx.MockTransport):
            governed = consume_dispatch(model, prompt, max_tokens)
        # Mock transports exercise the new governed candidate policy too.
        candidates = {"qwen3.7-flash", "qwen3.7-plus", "qwen3.7-max", "gemini-2.5-flash-lite"}
        extended = governed or (isinstance(self.transport, httpx.MockTransport) and model in candidates)
        if reasoning_effort not in {"low", "none"} or (reasoning_effort == "none" and (not require_rub_usage or model not in candidates)):
            raise LLMUnavailable("Reasoning candidate policy refused")
        if require_rub_usage and (type(max_tokens) is not int or not 0 < max_tokens <= (2048 if extended else 400)):
            raise LLMBudgetExceeded("Публичный запрос ограничен 400 выходными токенами; новый резерв требует отдельного согласования.")
        if not self.api_key:
            raise LLMUnavailable("JARVIS_LLM_API_KEY is not set")

        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if messages is not None:
            payload['messages']=messages
            if tools:
                payload.update(tools=tools,parallel_tool_calls=False)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if require_rub_usage:
            payload["reasoning"] = {"effort": reasoning_effort, "exclude": True}

        self._mark('preflight_passed')
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-AITunnel-Log": "off",
        }
        diagnostic: dict[str, object] = {"model": model, "http_status": None, "finish_reason": "unknown",
            "content_type": "unknown", "content_length": None, "completion_tokens": None,
            "reasoning_tokens": None, "reasoning_present": None, "cost_rub": None, "error_type": "transport"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport, trust_env=False) as client:
                self._mark('http_request_started')
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except httpx.HTTPError:
            raise LLMUnavailable("openai_compat request outcome uncertain", diagnostics=diagnostic) from None

        self._mark('http_response_received', http_status=resp.status_code)
        diagnostic.update(http_status=resp.status_code, error_type="http")
        import re
        request_id = resp.headers.get("x-request-id")
        self.provider_request_id = request_id if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id) else None
        if resp.status_code >= 400:
            diagnostic.update(parse_error(resp.status_code, resp.content))
        if resp.status_code >= 500:
            raise LLMUnavailable(f"openai_compat {resp.status_code}", diagnostics=diagnostic)
        if resp.status_code >= 400:
            raise LLMUnavailable(f"openai_compat {resp.status_code}", diagnostics=diagnostic)

        try:
            data = resp.json()
            if not isinstance(data, dict):
                raise TypeError("response must be an object")
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMUnavailable("openai_compat invalid response", diagnostics={**diagnostic, "error_type": "response"}) from None
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        content = message.get("content")
        self.response_content = content if isinstance(content, str) else None
        finish = choice.get("finish_reason")
        finish = finish if isinstance(finish, str) or finish is None else "unknown"
        diagnostic.update(finish_reason=finish if finish in {"stop", "length", "tool_calls", "content_filter"} else "unknown",
            content_type="string" if isinstance(content, str) else "null" if content is None else "unexpected",
            content_length=len(content) if isinstance(content, str) else None,
            reasoning_present=bool(message.get("reasoning") or message.get("reasoning_details") or message.get("reasoning_content")))
        usage = data.get("usage")
        if not isinstance(usage, dict):
            raise LLMUsageUnavailable("openai_compat usage unavailable", diagnostics={**diagnostic, "error_type": "usage"})
        tokens_in = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens")
        if (type(tokens_in) is not int or type(tokens_out) is not int or
                tokens_in <= 0 or tokens_out < 0 or tokens_out > max_tokens):
            raise LLMUsageUnavailable("openai_compat usage unavailable", diagnostics={**diagnostic, "error_type": "usage"})
        total = usage.get("total_tokens")
        diagnostic["completion_tokens"] = tokens_out
        if "total_tokens" in usage and (type(total) is not int or total != tokens_in + tokens_out):
            raise LLMUsageUnavailable("openai_compat inconsistent usage", diagnostics={**diagnostic, "error_type": "usage"})
        if require_rub_usage:
            try:
                cost = Decimal(str(usage["cost_rub"]))
                balance = Decimal(str(usage["balance"])) if usage.get("balance") is not None else None
                reasoning = usage.get("completion_tokens_details", {}).get("reasoning_tokens")
                cached = usage.get("prompt_tokens_details", {}).get("cached_tokens")
                if (not cost.is_finite() or cost < 0 or (balance is not None and (not balance.is_finite() or balance < 0))
                        or (reasoning is not None and (type(reasoning) is not int or not 0 <= reasoning <= tokens_out))
                        or (cached is not None and (type(cached) is not int or not 0 <= cached <= tokens_in))
                        or data.get("model") != model):
                    raise ValueError("Invalid RUB usage")
                self.rub_usage = {"cost_rub": str(cost), "balance": str(balance) if balance is not None else None,
                                  "reasoning_tokens": reasoning, "cached_tokens": cached}
            except (KeyError, ValueError, TypeError, AttributeError, InvalidOperation):
                raise LLMUsageUnavailable("AITunnel usage invalid", diagnostics={**diagnostic, "error_type": "usage"}) from None
        has_tools = bool(message.get("tool_calls") or message.get("function_call"))
        if messages is not None:
            # Native tool turns may legitimately contain no textual answer.
            calls=message.get('tool_calls') or []
            allowed={str(tool.get('function',{}).get('name')) for tool in tools or []}
            valid=(isinstance(calls,list) and len(calls)<=4 and
                all(isinstance(call,dict) and call.get('type')=='function' and
                    isinstance(call.get('id'),str) and 1<=len(call['id'])<=100 and
                    isinstance(call.get('function'),dict) and call['function'].get('name') in allowed and
                    isinstance(call['function'].get('arguments'),str) and len(call['function']['arguments'])<=4000
                    for call in calls))
            logging=resp.headers.get('x-aitunnel-log','off')
            if (not valid or logging!='off' or data.get('model')!=model
                    or finish != ('tool_calls' if calls else 'stop')
                    or len({call['id'] for call in calls})!=len(calls)
                    or (not calls and (not isinstance(content,str) or not content.strip()))):
                self.response_diagnostics={**diagnostic,'error_type':'conversation_contract'}
                raise LLMResponseUnavailable(self.response_diagnostics)
            self.response_message={'role':'assistant','content':content if isinstance(content,str) else None}
            if calls:
                self.response_message['tool_calls']=calls
            self.response_diagnostics={**diagnostic,'prompt_tokens':tokens_in,'error_type':'unknown'}
            self._mark('response_validated', http_status=resp.status_code,
                       finish_reason=diagnostic.get('finish_reason'), usage_present=True)
            return json.dumps(self.response_message,ensure_ascii=False),tokens_in,tokens_out
        error = ("tool_calls" if has_tools else "length" if finish == "length" else
                 "content_type" if not isinstance(content, str) else "empty_content" if not content.strip() else
                 "finish_reason" if finish not in {None, "stop"} else None)
        if error:
            raise LLMResponseUnavailable({"model": model, "http_status": resp.status_code,
                "finish_reason": finish if finish in {"stop", "length", "tool_calls", "content_filter"} else "unknown",
                "content_type": "string" if isinstance(content, str) else "null" if content is None else "unexpected",
                "content_length": len(content) if isinstance(content, str) else None,
                "completion_tokens": tokens_out,
                "prompt_tokens": tokens_in,
                "reasoning_tokens": self.rub_usage.get("reasoning_tokens") if self.rub_usage else None,
                "reasoning_present": bool(message.get("reasoning") or message.get("reasoning_details") or message.get("reasoning_content")),
                "cost_rub": self.rub_usage.get("cost_rub") if self.rub_usage else None, "error_type": error})
        assert isinstance(content, str)
        self._mark('response_validated', http_status=resp.status_code,
                   finish_reason=diagnostic.get('finish_reason'), usage_present=True)
        diagnostic.update(prompt_tokens=tokens_in, error_type="unknown",
                          reasoning_tokens=self.rub_usage.get("reasoning_tokens") if self.rub_usage else None,
                          cost_rub=self.rub_usage.get("cost_rub") if self.rub_usage else None)
        self.response_diagnostics = diagnostic
        return content, tokens_in, tokens_out
