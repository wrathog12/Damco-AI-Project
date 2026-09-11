"""
Pass B — Postgres rows → embeddings → Qdrant.

Decoupled from Pass A on purpose: this never touches the myScheme API, so the
index can be rebuilt as often as the embedding model or the search document
changes without re-crawling a rate-limited government service.

Incremental by `scheme_embedding_state`: a scheme is re-embedded only when its
`content_hash` differs from the hash last embedded *with the current model*. So
changing `embedding_model` correctly invalidates everything (vectors from two
models are not comparable), while a re-run with no data change costs nothing.

Point ids are `Scheme.id` — the Postgres primary key. Qdrant needs an integer or
UUID, and reusing the PK means a scheme keeps its point across rebuilds and the
join back to Postgres is trivial.
"""
import time
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from embeddings.document import build_search_document
from embeddings.gemini import GeminiEmbedder
from models import Scheme, SchemeEmbeddingState
from search import qdrant_index as qi
from search.sparse import encode_documents

# Payload kept in Qdrant. Only what filtering or card rendering needs — the full
# record stays in Postgres, so this is not a second source of truth.
_PAYLOAD_FIELDS = (
    "scheme_id", "scheme_name", "scheme_short_title", "state", "states",
    "level", "category", "categories", "subcategories", "tags",
    "target_beneficiaries", "age_min", "age_max", "income_max", "gender",
    "caste", "occupation", "disability", "bpl_card", "state_residence",
    "minority", "student", "marital_status",
)


def _payload(scheme: Scheme) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in _PAYLOAD_FIELDS:
        value = getattr(scheme, field, None)
        # Numeric(14,2) comes back as Decimal, which is not JSON-serialisable.
        if field == "income_max" and value is not None:
            value = float(value)
        payload[field] = value
    # Filtering on a boolean is cheaper and clearer than on a nullable timestamp.
    payload["delisted"] = scheme.delisted_at is not None
    return payload


class Indexer:
    def __init__(
        self,
        *,
        embedder: GeminiEmbedder,
        batch_size: int = 32,
        force: bool = False,
        include_delisted: bool = True,
    ) -> None:
        self.embedder = embedder
        self.batch_size = batch_size
        self.force = force
        # De-listed schemes stay indexed but filtered out by default, so an old
        # card link keeps resolving instead of 404ing.
        self.include_delisted = include_delisted
        self.counts = {"considered": 0, "embedded": 0, "skipped": 0, "upserted": 0}

    async def _to_embed(self, session) -> list[Scheme]:
        """Schemes whose current content is not yet embedded with this model."""
        stmt = select(Scheme)
        if not self.include_delisted:
            stmt = stmt.where(Scheme.delisted_at.is_(None))
        schemes = list((await session.execute(stmt)).scalars())
        self.counts["considered"] = len(schemes)

        if self.force:
            return schemes

        state = {
            pk: hash_
            for pk, hash_ in (await session.execute(
                select(SchemeEmbeddingState.scheme_pk,
                       SchemeEmbeddingState.content_hash)
                .where(SchemeEmbeddingState.model == self.embedder.model)
            )).all()
        }
        todo = [s for s in schemes if state.get(s.id) != s.content_hash]
        self.counts["skipped"] = len(schemes) - len(todo)
        return todo

    async def _index_batch(self, session, client, collection: str,
                           batch: Sequence[Scheme], now) -> None:
        documents = [build_search_document(s) for s in batch]

        # Dense is a paid network call per document; sparse is local. Both are
        # order-preserving, which is what lets us zip them back to the schemes.
        dense = await self.embedder.embed_documents(documents)
        sparse = encode_documents(documents)
        self.counts["embedded"] += len(batch)

        points = [
            qi.to_point(point_id=scheme.id, dense=vec,
                        sparse_indices=sp[0], sparse_values=sp[1],
                        payload=_payload(scheme))
            for scheme, vec, sp in zip(batch, dense, sparse)
        ]
        await qi.upsert_points(client, collection, points)
        self.counts["upserted"] += len(points)

        # Only record state *after* a successful upsert, so an interrupted run
        # re-embeds rather than claiming work it did not finish.
        rows = [{"scheme_pk": s.id, "model": self.embedder.model,
                 "content_hash": s.content_hash, "embedded_at": now}
                for s in batch]
        stmt = insert(SchemeEmbeddingState).values(rows)
        await session.execute(stmt.on_conflict_do_update(
            constraint="uq_scheme_embedding_model",
            set_={"content_hash": stmt.excluded.content_hash,
                  "embedded_at": stmt.excluded.embedded_at}))
        await session.commit()

    async def run(self, session, client, *, recreate: bool = False) -> dict:
        from sqlalchemy import func

        started = time.perf_counter()
        collection = await qi.ensure_collection(
            client, dim=self.embedder.dim, recreate=recreate)

        todo = await self._to_embed(session)
        print(f"[index] {self.counts['considered']} schemes, "
              f"{len(todo)} to embed, {self.counts['skipped']} already current "
              f"(model {self.embedder.model}, {self.embedder.dim}d)")

        now = func.now()
        for start in range(0, len(todo), self.batch_size):
            batch = todo[start:start + self.batch_size]
            await self._index_batch(session, client, collection, batch, now)
            done = min(start + self.batch_size, len(todo))
            elapsed = time.perf_counter() - started
            rate = done / elapsed if elapsed else 0
            print(f"\r[index] {done}/{len(todo)}  {rate:5.1f} schemes/s  "
                  f"ETA {(len(todo)-done)/rate/60 if rate else 0:5.1f} min  "
                  f"reqs={self.embedder.stats['requests']} "
                  f"retries={self.embedder.stats['retries']} "
                  f"429s={self.embedder.stats['rate_limited']}",
                  end="", flush=True)
        if todo:
            print()

        self.counts["wall_seconds"] = round(time.perf_counter() - started, 1)
        self.counts["collection"] = collection
        return self.counts

    def report(self) -> None:
        print("\n-- Index report ----------------------------------")
        for key in ("considered", "skipped", "embedded", "upserted"):
            print(f"  {key:<12} {self.counts.get(key, 0)}")
        print(f"  api calls    {self.embedder.stats['requests']} "
              f"(retries {self.embedder.stats['retries']}, "
              f"429s {self.embedder.stats['rate_limited']})")
        print(f"  wall time    {self.counts.get('wall_seconds', 0)}s")
        print(f"  collection   {self.counts.get('collection')}")
