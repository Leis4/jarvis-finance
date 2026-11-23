"""Policy-based Qwen role registry and server-side model router.

One cognitive engine, one memory, one budget: roles only describe bounded
behaviour. The router never escalates on provider errors and never grants Max
without an explicit owner scope.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Model = Literal['qwen3.7-flash', 'qwen3.7-plus', 'qwen3.7-max']


@dataclass(frozen=True)
class Role:
    id: str
    title: str
    model: Model
    purpose: Literal['dialogue', 'classification', 'research', 'maintenance', 'reserve']
    consent_scope: str
    max_turns: int
    max_cost_rub: str
    max_output_tokens: int
    tools: tuple[str, ...]
    trigger: str


FLASH_ROLES = (
    Role('F01', 'Everyday chat', 'qwen3.7-flash', 'dialogue', 'financial_read_tools', 5, '0.50', 1024,
         ('financial_query', 'confirmed_memory'), 'owner question'),
    Role('F03', 'Transaction classifier', 'qwen3.7-flash', 'classification', 'category_proposal', 2, '0.50', 512,
         ('classify_transaction',), 'unknown transaction'),
    Role('F06', 'Daily briefing writer', 'qwen3.7-flash', 'dialogue', 'scheduled_ai', 2, '0.50', 512,
         ('financial_query',), 'scheduled local briefing'),
    Role('F08', 'Memory candidate extractor', 'qwen3.7-flash', 'maintenance', 'scheduled_ai', 1, '0.20', 256,
         ('confirmed_memory',), 'explicit memory review'),
)
PLUS_ROLES = (
    Role('P01', 'Deep financial chat', 'qwen3.7-plus', 'dialogue', 'financial_read_tools', 5, '2.00', 2048,
         ('financial_query', 'confirmed_memory'), 'complex financial question'),
    Role('P02', 'Merchant researcher', 'qwen3.7-plus', 'research', 'public_research', 3, '1.00', 1024,
         ('public_organization_research',), 'unknown merchant with public scope'),
    Role('P03', 'Weekly analyst', 'qwen3.7-plus', 'dialogue', 'scheduled_plus', 4, '2.00', 2048,
         ('financial_query',), 'weekly scheduled review'),
    Role('P05', 'Recurring payments analyst', 'qwen3.7-plus', 'dialogue', 'scheduled_plus', 4, '2.00', 2048,
         ('financial_query',), 'recurring payment question'),
    Role('P06', 'Scenario planner', 'qwen3.7-plus', 'dialogue', 'financial_read_tools', 4, '2.00', 2048,
         ('financial_query',), 'what-if scenario'),
)
MAX_ROLES = (
    Role('M01', 'Monthly strategist', 'qwen3.7-max', 'reserve', 'scheduled_max', 6, '6.00', 3072,
         ('financial_query',), 'monthly strategic review'),
    Role('M02', 'Forecast auditor', 'qwen3.7-max', 'reserve', 'scheduled_max', 5, '6.00', 3072,
         ('financial_query',), 'forecast audit'),
    Role('M04', 'Long-term planner', 'qwen3.7-max', 'reserve', 'scheduled_max', 6, '8.00', 3072,
         ('financial_query',), 'quarterly strategy'),
)
REGISTRY = {role.id: role for role in (*FLASH_ROLES, *PLUS_ROLES, *MAX_ROLES)}


@dataclass(frozen=True)
class RouteDecision:
    role_id: str
    model: Model
    reason: str
    allowed: bool
    max_turns: int
    max_cost_rub: str
    purpose: str
    consent_scope: str


COMPLEX = re.compile(r'почему|сравн|измен|сценар|прогноз|цел[ьи]|подписк|недел|месяц|категор|'
                     r'вклад|сократ|бюджет|достижим|анализ', re.IGNORECASE)
STRATEGIC = re.compile(r'стратег|квартал|долгосроч|годов|месячный обзор|финансовый план', re.IGNORECASE)
CLASSIFY = re.compile(r'категор|классифиц|мерчант|магазин|организац', re.IGNORECASE)
SIMPLE = re.compile(r'привет|как дела|спасибо|что ты умеешь|помощь', re.IGNORECASE)


def route(message: str, *, consent_enabled: bool = True, financial_tools: bool = True,
          scheduled: bool = False, strategic_allowed: bool = False,
          manual_model: Model | None = None, data_available: bool = True) -> RouteDecision:
    """Choose the cheapest sufficient role; never escalate because of an error."""
    text = str(message or '')
    if not consent_enabled:
        return _denied('F01', 'cloud consent disabled')
    if manual_model == 'qwen3.7-max':
        if not strategic_allowed:
            return _denied('M01', 'max requires separate owner scope')
        role = REGISTRY['M01']
        return _decision(role, 'explicit owner max request')
    if STRATEGIC.search(text):
        role = REGISTRY['M01'] if scheduled else REGISTRY['M04']
        if not strategic_allowed:
            return _denied(role.id, 'max requires separate owner scope')
        return _decision(role, 'strategic scope')
    if not data_available:
        return _decision(REGISTRY['F01'], 'insufficient local data; no escalation')
    if CLASSIFY.search(text) and not COMPLEX.search(text):
        return _decision(REGISTRY['F03'], 'classification request')
    if COMPLEX.search(text):
        return _decision(REGISTRY['P01'], 'multi-factor financial question')
    if SIMPLE.search(text):
        return _decision(REGISTRY['F01'], 'simple operational chat')
    return _decision(REGISTRY['F01'], 'default operational chat')


def _decision(role: Role, reason: str) -> RouteDecision:
    return RouteDecision(role.id, role.model, reason, True, role.max_turns,
                         role.max_cost_rub, role.purpose, role.consent_scope)


def _denied(role_id: str, reason: str) -> RouteDecision:
    role = REGISTRY[role_id]
    return RouteDecision(role.id, role.model, reason, False, role.max_turns,
                         role.max_cost_rub, role.purpose, role.consent_scope)
