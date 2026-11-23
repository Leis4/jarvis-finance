"""Owner-screened organization search; pages never become instructions or labels."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from jarvis.finance.personal_cfo import PersonalCFO
from jarvis.knowledge.discovery import searxng
from jarvis.knowledge.public_sources import PinnedHTTPS, checked_url


class OrganizationSearch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    organization: str = Field(min_length=2, max_length=80)
    domains: list[str] = Field(min_length=1, max_length=3)
    public_organization_confirmed: Literal[True]

    @model_validator(mode='after')
    def screen(self) -> OrganizationSearch:
        if (not re.fullmatch(r'[\wА-Яа-яЁё &«»().-]+', self.organization)
                or re.search(r'\d{5}|(?i:pin|otp|пароль|счет|счёт|перевод|ignore|инструкц|отмен[аи])', self.organization)
                or re.search(r'\b[А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+\b', self.organization)):
            raise ValueError('Only an explicitly screened public organization name is allowed')
        for domain in self.domains:
            if not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', domain):
                raise ValueError('Explicit public source domains required')
        return self


async def research(cfo: PersonalCFO, request: OrganizationSearch, *,
                   transport: httpx.MockTransport | None = None) -> dict[str, Any]:
    """Search one approved name through existing SearXNG; unmetered paid search stays off."""
    endpoint = os.environ.get('JARVIS_CFO_FREE_SEARCH_ENDPOINT', '')
    if not endpoint or os.environ.get('JARVIS_CFO_FREE_SEARCH_APPROVED') != '1':
        return {'status': 'disabled', 'reason': 'Free search endpoint not approved; automatic paid search disabled', 'paid_cost_rub': '0'}
    identity = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    key = 'cfo.merchant_research:' + identity
    prior = cfo.db.get_setting(key, None)
    if prior:
        return {**prior, 'cached': True}
    if not cfo.lock.acquire(blocking=False):
        return {'status': 'busy'}
    try:
        day = datetime.now(UTC).date().isoformat()
        counter = 'cfo.research_attempts:' + day
        count = cfo.db.get_setting(counter, 0)
        if count >= 3:
            return {'status': 'rate_limited'}
        cfo.db.set_setting(counter, count+1)
        async with httpx.AsyncClient(transport=transport or PinnedHTTPS(), trust_env=False,
                                    follow_redirects=False, timeout=10) as client:
            result = await searxng(client, endpoint, request.organization, frozenset(request.domains))
            sources = []
            for url in result.get('urls', [])[:2]:
                checked_url(url, frozenset(request.domains))
                response = await client.get(url, headers={'Accept-Encoding': 'identity'})
                if response.status_code == 200:
                    sources.append({'url': url, 'content_sha256': hashlib.sha256(response.content).hexdigest(),
                        'observed_at': datetime.now(UTC).isoformat(), 'claim': 'public_page_reachable_only'})
        receipt = {'status': 'review', 'organization': request.organization, 'sources': sources,
            'search': result, 'observed_at': datetime.now(UTC).isoformat(),
            'reason': 'Business identity and purchase purpose require owner review; snippets/pages are untrusted data',
            'paid_cost_rub': '0', 'cached': False, 'financial_category_applied': False}
        cfo.db.set_setting(key, receipt)
        return receipt
    except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError):
        return {'status': 'unavailable', 'reason': 'Public source verification failed; no retry'}
    finally:
        cfo.lock.release()
