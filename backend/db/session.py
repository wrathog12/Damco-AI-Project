"""
Async SQLAlchemy engine and session factory.

The engine is created lazily rather than at import time so that importing
`config` (or running Alembic) never opens a connection pool as a side effect.
Hand the session factory to the voice pipeline via Pipecat's `app_resources`
DI slot — never via a module-level global that leaks across sessions.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def make_engine() -> AsyncEngine:
    """A fresh engine with the standard pool settings.

    asyncpg connections belong to the event loop that opened them, so anything
    running on its own loop — an ingestion script, a test harness — must own its
    own engine rather than sharing the process-wide one from `get_engine()`. This
    exists so that "own your engine" does not also mean "duplicate the pool
    config".

    The server itself calls this once, through `services.resources.create()`, in
    the FastAPI lifespan.
    """
    return create_async_engine(
        settings.database_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,  # survive Postgres restarts / idle timeouts
        echo=False,
    )


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commits on success, rolls back on any exception."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — `db: AsyncSession = Depends(get_db)`."""
    async with session_scope() as session:
        yield session


async def dispose_engine() -> None:
    """Close the pool on application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
