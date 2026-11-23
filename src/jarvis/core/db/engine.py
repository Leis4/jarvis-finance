from collections.abc import Generator
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from jarvis.core.config import settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{settings.db_path}",
            connect_args={"check_same_thread": False},
        )
    return _engine


def init_db() -> None:
    import jarvis.core.db.models.llm_calls  # noqa: F401

    SQLModel.metadata.create_all(get_engine())


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session