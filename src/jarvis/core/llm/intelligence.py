"""Budgeted purpose router over the existing provider, journal and fact validators."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

import httpx

from jarvis.core.llm.budget_store import (
    BudgetReservationDenied,
    BudgetStore,
    UsageReceipt,
)
from jarvis.core.llm.errors import LLMError, LLMUnavailable
from jarvis.core.llm.governance import canonical_journal, paid_dispatch
from jarvis.core.llm.pricing import aitunnel_price_evidence, refresh_aitunnel_catalog
from jarvis.core.llm.providers.openai_compat import OpenAICompatProvider

MODELS = ("qwen3.7-flash", "qwen3.7-plus", "gemini-2.5-flash-lite")


class Intelligence:
    """Single in-flight request, atomic accounting and one cost-known fallback."""

    def __init__(self, journal: BudgetStore, pricing: Path):
        self.journal, self.pricing = journal, pricing

    async def turn(self, ident: str, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
                   purpose: str = 'dialogue', max_output: int = 1024,
                   provider_factory: Callable[[], OpenAICompatProvider] = OpenAICompatProvider,
                   ceiling: Decimal = Decimal(2), role_id: str | None = None,
                   job_id: str | None = None) -> dict[str, Any]:
        """One exact-model native tool turn; no retry, fallback or unreserved dispatch."""
        from jarvis.core.llm.providers.openai_compat import chat_fingerprint_payload
        if model not in {'qwen3.7-flash','qwen3.7-plus','qwen3.7-max'} or not 128<=max_output<=2048:
            raise ValueError('Explicit bounded Qwen model required')
        prompt=chat_fingerprint_payload(messages,tools)
        if self.journal.request_status(ident):
            raise LLMUnavailable('Request already attempted; inspect saved task instead of retry')
        evidence=aitunnel_price_evidence(self.pricing,model,now=datetime.now(UTC),
            input_cap=len(prompt.encode())+512,output_cap=max_output,conversation=True)
        reserve=evidence.reserved_rub.quantize(Decimal('.01'),rounding=ROUND_CEILING)
        if reserve>ceiling:
            raise BudgetReservationDenied('Request exceeds explicit cost ceiling')
        provider=provider_factory()
        mock=isinstance(provider.transport,httpx.MockTransport)
        if not mock:
            if self.journal.path.resolve()!=canonical_journal():
                raise LLMUnavailable('Canonical journal required')
            status=await provider.owner_key_status(owner_action=True,model=model,global_policy=True)
            async with httpx.AsyncClient(timeout=15,trust_env=False) as client:
                response=await client.get(provider.base_url+'/aitunnel/balance',
                    headers={'Authorization':'Bearer '+provider.api_key,'X-AITunnel-Log':'off'})
            response.raise_for_status()
            status['account_balance_rub']=str(response.json()['balance'])
            if status['status']!='ready':raise LLMUnavailable('Provider key does not permit selected model')
            self.journal.provider_snapshot(status)
        self.journal.enable_purpose_limits()
        self.journal.reserve(ident,reserve,'500','500',evidence.evidence_sha256,
            now=datetime.now(UTC),purpose=purpose)
        if role_id and job_id:
            self.journal.attribute(ident, role_id, job_id)
        self.journal.task_metadata(ident,purpose,model)
        self.journal.mark_attempted(ident,model=model)
        started=time.monotonic()
        try:
            with paid_dispatch(self.journal,ident,model,prompt,max_output):
                content,tin,tout=await provider.complete(model,prompt,max_output,require_rub_usage=True,
                    reasoning_effort='none',messages=messages,tools=tools)
        except BaseException as exc:
            usage=provider.rub_usage
            settled=None
            if usage is not None and usage.get('cost_rub') is not None:
                self.journal.settle(ident,str(usage['cost_rub']),now=datetime.now(UTC))
                settled=str(usage['cost_rub'])
            else:self.journal.mark_uncertain(ident)
            self.write_turn_diagnostics(ident,model,provider,started,outcome='failed',error=exc,
                                        settled_rub=settled)
            raise
        usage=provider.rub_usage
        if usage is None or provider.response_message is None:
            self.journal.mark_uncertain(ident)
            self.write_turn_diagnostics(ident,model,provider,started,outcome='failed',
                                        error=LLMUnavailable('unknown_usage'))
            raise LLMUnavailable('Unknown usage; no automatic retry')
        cost=Decimal(str(usage['cost_rub']))
        self.journal.settle(ident,cost,now=datetime.now(UTC),receipt=UsageReceipt(model,tin,tout,
            hashlib.sha256(prompt.encode()).hexdigest(),hashlib.sha256(content.encode()).hexdigest()))
        self.journal.task_metadata(ident,purpose,model,request=provider.provider_request_id,
            latency=int((time.monotonic()-started)*1000),finish=str(provider.response_diagnostics.get('finish_reason')),
            quality='TURN_RECEIVED')
        self.write_turn_diagnostics(ident,model,provider,started,outcome='completed',
                                    settled_rub=str(cost))
        if cost>reserve:raise LLMUnavailable('Actual cost exceeded reservation; halted')
        return {'message':provider.response_message,'model':model,'paid_cost_rub':str(cost),
            'mock':mock,'tokens_in':tin,'tokens_out':tout,'latency_ms':int((time.monotonic()-started)*1000)}

    def write_turn_diagnostics(self, ident: str, model: str, provider: OpenAICompatProvider,
                               started: float, *, outcome: str, error: BaseException | None = None,
                               settled_rub: str | None = None) -> None:
        """Append safe stage diagnostics; never prompts, bodies, keys or headers."""
        path = self.journal.path.parent / 'turn-diagnostics.jsonl'
        last_event = provider.stage_events[-1] if provider.stage_events else {}
        base = {'ts': datetime.now(UTC).isoformat(), 'request_id': ident, 'model': model,
                'outcome': outcome, 'error_type': type(error).__name__ if error else None,
                'http_status': provider.response_diagnostics.get('http_status')
                              or last_event.get('http_status'),
                'provider_request_id': provider.provider_request_id,
                'finish_reason': provider.response_diagnostics.get('finish_reason'),
                'usage_present': provider.rub_usage is not None,
                'receipt_present': settled_rub is not None, 'settled_rub': settled_rub,
                'elapsed_ms': int((time.monotonic() - started) * 1000)}
        events = provider.stage_events or [{'stage': provider.last_stage or 'no_stage', 'at': started}]
        lines = [{**base, 'stage': event.get('stage'),
                  'stage_elapsed_ms': int((event.get('at', started) - started) * 1000),
                  'stage_http_status': event.get('http_status')} for event in events]
        lines.append({**base, 'stage': 'budget_settled' if settled_rub is not None else 'budget_uncertain',
                      'stage_elapsed_ms': base['elapsed_ms'], 'stage_http_status': base['http_status']})
        try:
            with path.open('a', encoding='utf-8') as handle:
                for line in lines:
                    handle.write(json.dumps(line, ensure_ascii=False) + '\n')
        except OSError:
            pass

    def save_response(self, ident: str, model: str, provider: OpenAICompatProvider, diagnostic: dict[str, Any]) -> None:
        directory = self.journal.path.parent / 'responses'
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / (ident + '.json')).open('x', encoding='utf-8') as saved:
            json.dump({'model': model, 'content': provider.response_content, 'usage': provider.rub_usage,
                'diagnostics': diagnostic or provider.response_diagnostics,
                'provider_request_id': provider.provider_request_id}, saved, ensure_ascii=False)

    @classmethod
    def configured(cls) -> Intelligence:
        from jarvis.core.config import settings
        if not settings.llm_api_key:
            raise LLMUnavailable('LLM key missing; no paid dispatch')
        path = canonical_journal()
        path.parent.mkdir(parents=True, exist_ok=True)
        store = BudgetStore(path, currency="RUB")
        # Migrate only this dedicated AI journal; never initialize FinanceDB here.
        if path.exists():
            with store._transaction() as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                columns={row[1] for row in conn.execute('PRAGMA table_info(cfo_ai_provider)')}
                governed = ({'cfo_ai_policy', 'cfo_ai_tasks', 'cfo_ai_cache', 'cfo_ai_provider',store.purpose_limits,store.purposes} <= tables
                    and 'account_balance' in columns)
            if not governed:
                backup = path.with_name("pre-global-" + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite')
                store.upgrade_existing(backup)
        else:
            store.initialize()
        store.enable_global()
        pricing = path.parent / ('catalog-' + datetime.now(UTC).strftime('%Y%m%d') + '.json')
        if not pricing.exists():
            refresh_aitunnel_catalog(pricing)
        return cls(store, pricing)

    async def generate(self, task: str, prompt: str, validate: Callable[[dict[str, Any]], dict[str, Any]], *,
                       ambiguous: bool = False, model: str | None = None, max_output: int = 1024,
                       provider_factory: Callable[[], OpenAICompatProvider] = OpenAICompatProvider,
                       fallback: bool = True, ceiling: Decimal = Decimal(1)) -> dict[str, Any]:
        """Dispatch screened generated prompts only; unknown spend always blocks fallback."""
        # Plus failed the fixed live benchmark (reasoning exhausted the output cap).
        # An independent accepted candidate is used; no claim of superior accuracy.
        selected = model or (MODELS[2] if ambiguous else MODELS[0])
        if selected == MODELS[1] and task != 'benchmark':
            return {'status': 'unavailable', 'code': 'candidate_not_accepted', 'paid_cost_rub': '0'}
        if (task not in {'classification', 'analysis', 'chat', 'benchmark'} or selected not in MODELS
                or not 128 <= max_output <= 2048 or not prompt or len(prompt.encode()) + 512 > 6000):
            return {'status': 'unavailable', 'code': 'bounded_request_required', 'paid_cost_rub': '0'}
        root = hashlib.sha256(('cfo-intelligence-v1:' + task + ':' + selected + ':' + str(max_output) + ':' + prompt).encode()).hexdigest()
        cached = self.journal.cached(root)
        if cached:
            return {**cached, 'cached': True, 'paid_cost_rub': '0'}
        total = Decimal(0)
        attempts: list[dict[str, Any]] = []
        candidates = [selected] + ([MODELS[2]] if fallback and selected != MODELS[2] else [])
        for index, candidate in enumerate(candidates):
            ident = root if index == 0 else hashlib.sha256((root + ':fallback').encode()).hexdigest()
            prior = self.journal.request_status(ident)
            if prior:
                return {'status': 'not_repeated', 'billing_status': prior['status'], 'paid_cost_rub': str(total)}
            try:
                price = aitunnel_price_evidence(self.pricing, candidate, now=datetime.now(UTC),
                    input_cap=len(prompt.encode()) + 512, output_cap=max_output)
            except LLMError:
                # Catalog preflight fails before dispatch: safe to choose the reserve model.
                if attempts:
                    break
                if index == 0 and len(candidates) > 1:
                    continue
                return {'status': 'unavailable', 'code': 'pricing_or_capabilities', 'paid_cost_rub': str(total)}
            reserve = price.reserved_rub.quantize(Decimal('.01'), rounding=ROUND_CEILING)
            if total + reserve > ceiling:
                return {'status': 'budget_refused', 'code': 'task_ceiling', 'paid_cost_rub': str(total)}
            provider = provider_factory()
            if not isinstance(provider.transport, httpx.MockTransport) and self.journal.path.resolve() != canonical_journal():
                return {'status': 'unavailable', 'code': 'canonical_journal_required', 'paid_cost_rub': str(total)}
            if not provider.api_key or provider.base_url != 'https://api.aitunnel.ru/v1':
                return {'status': 'unavailable', 'code': 'provider_not_configured', 'paid_cost_rub': str(total)}
            # Local owner mode: the existing RUB BudgetStore is the spend guard.
            # Provider key adequacy is checked by the request itself; no manual grant gate.
            try:
                self.journal.reserve(ident, reserve, '250', '250', price.evidence_sha256, now=datetime.now(UTC))
            except BudgetReservationDenied:
                return {'status': 'budget_refused', 'code': 'limit_inflight_or_unknown', 'paid_cost_rub': str(total)}
            self.journal.task_metadata(ident, task, candidate)
            self.journal.mark_attempted(ident, model=candidate)
            started = time.monotonic()
            content, tin, tout = '', 0, 0
            error = False
            diagnostic: dict[str, Any] = {}
            try:
                with paid_dispatch(self.journal, ident, candidate, prompt, max_output):
                    content, tin, tout = await provider.complete(candidate, prompt, max_output,
                        json_mode=True, require_rub_usage=True,
                        reasoning_effort='low' if candidate == MODELS[1] else 'none')
            except LLMError as exc:
                error = True
                diagnostic = getattr(exc, 'diagnostics', None) or {}
                tin, tout = int(diagnostic.get('prompt_tokens') or 0), int(diagnostic.get('completion_tokens') or 0)
            except BaseException:
                self.journal.mark_uncertain(ident)
                raise
            usage = provider.rub_usage
            elapsed = int((time.monotonic()-started)*1000)
            if not usage or usage.get('cost_rub') is None:
                self.journal.mark_uncertain(ident)
                self.save_response(ident, candidate, provider, diagnostic)
                self.journal.task_metadata(ident, task, candidate, request=provider.provider_request_id,
                    latency=elapsed, quality='UNKNOWN')
                return {'status': 'unknown', 'code': 'no_retry_cost_unknown', 'paid_cost_rub': None,
                    'known_cost_rub': str(total), 'request_id': ident, 'attempts': attempts}
            cost = Decimal(str(usage['cost_rub']))
            receipt = UsageReceipt(candidate, tin, tout, hashlib.sha256(prompt.encode()).hexdigest(),
                hashlib.sha256(content.encode()).hexdigest()) if tin else None
            reasoning_tokens, cached_tokens = usage.get('reasoning_tokens'), usage.get('cached_tokens')
            self.journal.settle(ident, cost, now=datetime.now(UTC), receipt=receipt,
                reasoning_tokens=reasoning_tokens if receipt and isinstance(reasoning_tokens, int) else None,
                cached_tokens=cached_tokens if receipt and isinstance(cached_tokens, int) else None)
            self.save_response(ident, candidate, provider, diagnostic)
            total += cost
            result: dict[str, Any] | None = None
            if not error and cost <= reserve and provider.response_diagnostics.get('finish_reason') == 'stop':
                try:
                    value = json.loads(content)
                    if not isinstance(value, dict):
                        raise TypeError('JSON object required')
                    result = validate(value)
                except (ValueError, KeyError, TypeError):
                    pass
            quality = 'ACCEPTED' if result is not None else 'QUALITY_NOT_ACCEPTED'
            finish = str((diagnostic or provider.response_diagnostics).get('finish_reason', 'unknown'))
            self.journal.task_metadata(ident, task, candidate, request=provider.provider_request_id,
                latency=elapsed, finish=finish, quality=quality)
            attempts.append({'model': candidate, 'request_id': ident, 'cost_rub': str(cost), 'tokens_in': tin,
                'tokens_out': tout, 'reasoning_tokens': usage.get('reasoning_tokens'), 'latency_ms': elapsed,
                'finish_reason': finish, 'quality': quality, 'provider_request_id': provider.provider_request_id})
            if result is not None:
                response = {'status': 'available', 'result': result, 'model': candidate, 'cached': False,
                    'paid_cost_rub': str(total), 'attempts': attempts, 'tokens_in': tin, 'tokens_out': tout}
                self.journal.cache_result(root, response)
                return response
            if cost > reserve:
                break  # Record the actual debit, then halt: no fallback after a tariff overrun.
        return {'status': 'quality_not_accepted', 'paid_cost_rub': str(total), 'attempts': attempts}
