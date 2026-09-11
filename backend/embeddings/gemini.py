"""
Gemini embedding client.

One measured quirk dictates the shape of this module:

    gemini-embedding-2 is a MULTIMODAL model, so `contents=[a, b, c]` means
    "one document made of three parts", not "three documents". It returns a
    single vector. Verified: sending 3 distinct texts returns 1 embedding;
    sending 100 returns 1. `gemini-embedding-001` batches normally (3 -> 3).

That is a silent-corruption trap, not an error: batching 100 schemes per call
yields one vector for all 100 glued together, and Qdrant would look perfectly
healthy while returning nonsense. So this module embeds exactly one document per
request and `_embed_one` asserts the response shape rather than trusting it.

The other thing that matters is **task_type asymmetry**. Corpus text must be
embedded as RETRIEVAL_DOCUMENT and queries as RETRIEVAL_QUERY; using one type
for both measurably degrades recall, and nothing in the response reveals the
mistake. Hence two separate methods rather than one with a flag callers forget.
"""
import asyncio
import random
from typing import Sequence

from google import genai
from google.genai import types

from config import settings

# Verified against the live API: native 3072, truncatable to 768 / 1536 / 3072
# via Matryoshka. Anything else is rejected.
SUPPORTED_DIMS = (768, 1536, 3072)


class EmbeddingError(RuntimeError):
    """The embedding API failed in a way worth surfacing."""


def _is_rate_limit(exc: BaseException) -> bool:
    """Google returns 429 / RESOURCE_EXHAUSTED for quota; treat 5xx as transient."""
    text = f"{type(exc).__name__}: {exc}"
    return any(token in text for token in
               ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL"))


class GeminiEmbedder:
    """Concurrent, retrying, one-document-per-request embedder."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        dim: int | None = None,
        concurrency: int = 8,
        attempts: int = 6,
    ) -> None:
        key = api_key or settings.gemini_api_key
        if not key:
            raise EmbeddingError(
                "GEMINI_API_KEY is empty — set it in the repo-root .env")
        self.model = model or settings.embedding_model
        self.dim = dim or settings.embedding_dim
        if self.dim not in SUPPORTED_DIMS:
            raise EmbeddingError(
                f"embedding_dim={self.dim} unsupported; pick one of {SUPPORTED_DIMS}")
        self._client = genai.Client(api_key=key)
        self._sem = asyncio.Semaphore(concurrency)
        self._attempts = attempts
        self.stats = {"requests": 0, "retries": 0, "rate_limited": 0}

    # ── the single-document primitive ───────────────────────────────────
    async def _embed_one(self, text: str, task_type: str) -> list[float]:
        cfg = types.EmbedContentConfig(
            output_dimensionality=self.dim, task_type=task_type)
        last: Exception | None = None

        for attempt in range(self._attempts):
            if attempt:
                # Jittered backoff; quota errors want seconds, not milliseconds.
                await asyncio.sleep(min(2 ** attempt, 60) * (0.5 + random.random()))
                self.stats["retries"] += 1
            try:
                async with self._sem:
                    resp = await self._client.aio.models.embed_content(
                        model=self.model, contents=text, config=cfg)
                self.stats["requests"] += 1
            except Exception as exc:                      # noqa: BLE001
                last = exc
                if _is_rate_limit(exc):
                    self.stats["rate_limited"] += 1
                    continue
                # Malformed input etc. — retrying verbatim cannot help.
                raise EmbeddingError(f"embed failed: {type(exc).__name__}: {exc}") from exc

            embeddings = resp.embeddings or []
            # Guard the multimodal quirk explicitly: if a future SDK version ever
            # starts batching, silently taking [0] would corrupt the index.
            if len(embeddings) != 1:
                raise EmbeddingError(
                    f"expected exactly 1 embedding, got {len(embeddings)} — "
                    "the one-document-per-request assumption no longer holds")
            values = embeddings[0].values
            if not values or len(values) != self.dim:
                raise EmbeddingError(
                    f"expected {self.dim} dims, got {len(values or [])}")
            return list(values)

        raise EmbeddingError(f"gave up after {self._attempts} attempts: {last}") from last

    # ── the two task types, kept separate on purpose ────────────────────
    async def embed_document(self, text: str) -> list[float]:
        return await self._embed_one(text, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        return await self._embed_one(text, "RETRIEVAL_QUERY")

    async def aclose(self) -> None:
        """Close the SDK's aiohttp session.

        Without this, a process that stops its event loop at shutdown races
        google-genai's own atexit close and prints "Task was destroyed but it is
        pending" — harmless, but it buries real errors in the same output.
        """
        closer = getattr(getattr(self._client, "aio", None), "aclose", None)
        if closer is not None:
            await closer()

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Many documents, concurrently — still one request each.

        Order is preserved, which callers rely on to zip results back to scheme
        ids. `asyncio.gather` guarantees that; `as_completed` would not.
        """
        return list(await asyncio.gather(
            *(self.embed_document(t) for t in texts)))
