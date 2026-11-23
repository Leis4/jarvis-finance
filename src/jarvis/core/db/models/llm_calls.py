from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LLMCall(SQLModel, table=True):
    __tablename__ = "llm_calls"

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=_utcnow, index=True)
    task: str = Field(index=True)
    model: str
    prompt_hash: str = Field(index=True)
    tokens_in: int
    tokens_out: int
    cached: bool = False
    cost_usd: float = 0.0
    latency_ms: int
    error: str | None = None