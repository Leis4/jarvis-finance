from enum import Enum

from pydantic import BaseModel


class TaskType(str, Enum):
    CATEGORIZE_TX = "CATEGORIZE_TX"        # категоризация транзакций
    SUMMARIZE_NEWS = "SUMMARIZE_NEWS"      # саммари новостей по тикеру
    ANALYZE_MARKET = "ANALYZE_MARKET"      # анализ рыночной ситуации
    PROPOSE_TRADE = "PROPOSE_TRADE"        # генерация предложения сделки
    DETECT_ANOMALY = "DETECT_ANOMALY"      # аномалии в тратах/котировках
    OTHER = "OTHER"


class LLMResult(BaseModel):
    content: str
    parsed: BaseModel | None = None
    model: str
    tokens_in: int
    tokens_out: int
    cached: bool
    cost_usd: float
    latency_ms: int