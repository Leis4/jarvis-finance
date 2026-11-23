from sqlalchemy.orm import DeclarativeBase


# Dedicated DeclarativeBase for jarvis.invest models (separate from finance)
class Base(DeclarativeBase):
    """SQLAlchemy declarative base for invest package."""
