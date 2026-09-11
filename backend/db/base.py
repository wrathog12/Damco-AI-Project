"""
Declarative base for all ORM models.

Kept in its own module so Alembic's env.py can import `Base.metadata`
without pulling in the async engine (and therefore the event loop).
"""
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Common base — every table inherits naming conventions from here."""


class TimestampMixin:
    """`created_at` / `updated_at`, maintained by the database itself."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
