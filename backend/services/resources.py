"""
The Postgres pool, Qdrant client and embedder the tools share.

One `Resources` is built per process, in the FastAPI lifespan, and handed to
every voice session as Pipecat's `app_resources` — so a tool handler reaches the
database through `params.app_resources`, never through a module global. That is
cross-cutting rule 2, and it is why `create()` returns the object instead of
stashing it: a caller that cannot name where its resources came from is a caller
that will eventually share them with the wrong session.

The pool, the Qdrant client and the embedder are all bound to the event loop
that created them, which is now simply the server's loop — P1 needed a separate
loop thread because the tools were synchronous, and that scaffolding is gone.
"""
import asyncio
from dataclasses import dataclass, field

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from config import settings
from db.session import make_engine
from embeddings.gemini import GeminiEmbedder
from search import qdrant_index as qi


@dataclass
class Resources:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    qdrant: AsyncQdrantClient
    collection: str
    # Optional: the tools must keep working for structured lookups even with no
    # GEMINI_API_KEY, so a missing embedder degrades semantic search rather than
    # taking the whole data layer down. See `services.schemes.search`.
    embedder: GeminiEmbedder | None
    embedder_error: str | None = None
    # The corpus' own `state` / `category` spellings, read once from Postgres by
    # `schemes._vocabulary`. Cached here rather than in a module global so it
    # cannot outlive the pool it was read through.
    vocabulary: dict[str, tuple[str, ...]] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def close(self) -> None:
        for label, closer in (("qdrant", self.qdrant.close),
                              ("embedder", self.embedder.aclose
                               if self.embedder else None)):
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:                          # noqa: BLE001
                print(f"[data] {label} close failed: {exc}")
        await self.engine.dispose()


async def create() -> Resources:
    """Open the pool, the index client and the embedder. Call once per process."""
    embedder: GeminiEmbedder | None = None
    error: str | None = None
    try:
        embedder = GeminiEmbedder(concurrency=4)
    except Exception as exc:                                  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        print(f"[data] embeddings unavailable, semantic search disabled: {error}")

    engine = make_engine()
    return Resources(
        engine=engine,
        session_factory=async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession),
        qdrant=qi.make_client(),
        collection=settings.qdrant_collection,
        embedder=embedder,
        embedder_error=error,
    )
