from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from jarvis.invest.config.settings import get_settings

_SETTINGS = get_settings()
_ENGINE = create_engine(_SETTINGS.database_url, future=True, pool_pre_ping=True)
_SessionLocal = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False, future=True)


def get_engine() -> Any:
    return _ENGINE


@contextmanager
def session_scope() -> Iterator[Session]:
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
