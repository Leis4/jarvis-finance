"""Insights data models."""
from dataclasses import dataclass
from enum import Enum


class InsightType(str, Enum):
    TREND_UP = "trend_up"          # категория растёт
    TREND_DOWN = "trend_down"      # категория падает
    SPIKE = "spike"                # резкий скачок
    UNUSED_SUB = "unused_sub"      # подписка без использования
    OVER_LIMIT = "over_limit"      # выше лимита


@dataclass
class Insight:
    type: InsightType
    category: str
    current: float
    previous: float
    change_pct: float
    message: str              # готовая фраза
    suggestion: str | None    # что делать
    priority: int = 5         # 1=highest, 10=lowest
    events_count: int = 0     # количество событий (для unused_sub = списания)
    monthly_cost: float = 0.0 # вычисленная месячная стоимость


@dataclass
class InsightReport:
    generated_at: str
    period_days: int
    insights: list[Insight]
    llm_summary: str | None = None
    markov_patterns: list[str] | None = None

    @property
    def top_insights(self) -> list[Insight]:
        return sorted(self.insights, key=lambda x: x.priority)[:5]
