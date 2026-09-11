"""
Build the Qdrant hybrid index from Postgres.

    python backend/scripts/build_index.py --limit 20     # smoke test, ~$0.002
    python backend/scripts/build_index.py                # incremental
    python backend/scripts/build_index.py --recreate      # drop + rebuild
    python backend/scripts/build_index.py --estimate      # cost only, no calls

Re-running is cheap: only schemes whose `content_hash` is not already embedded
with the configured model are sent to the API. Changing `EMBEDDING_MODEL` or
`EMBEDDING_DIM` invalidates every vector, so that path needs `--recreate`.
"""
import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

sys.stdout.reconfigure(encoding="utf-8")

from config import settings                                    # noqa: E402
from db.session import dispose_engine, session_scope           # noqa: E402
from embeddings.document import build_search_document          # noqa: E402
from embeddings.gemini import GeminiEmbedder                   # noqa: E402
from ingestion.index_schemes import Indexer                    # noqa: E402
from search import qdrant_index as qi                          # noqa: E402

# gemini-embedding-2 text input, standard tier. Batch API is half this.
USD_PER_1M_TOKENS = 0.20


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=None,
                   help="only index the first N schemes needing work")
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--concurrency", type=int, default=8,
                   help="parallel embedding requests")
    p.add_argument("--force", action="store_true",
                   help="re-embed even if content_hash is already current")
    p.add_argument("--recreate", action="store_true",
                   help="drop and recreate the collection (required after a "
                        "model or dimension change)")
    p.add_argument("--estimate", action="store_true",
                   help="print token/cost estimate and exit without embedding")
    p.add_argument("--exclude-delisted", action="store_true",
                   help="do not index schemes that have been de-listed")
    return p.parse_args()


async def estimate() -> int:
    """Cost preview from the real documents, before spending anything."""
    from sqlalchemy import select

    from models import Scheme

    async with session_scope() as session:
        schemes = list((await session.execute(select(Scheme))).scalars())

    docs = [build_search_document(s) for s in schemes]
    chars = sum(len(d) for d in docs)
    # ~4 chars/token for English; the search document is English-only.
    tokens = chars / 4
    print(f"  schemes          {len(docs)}")
    print(f"  document chars   {chars:,} (avg {chars // max(len(docs),1):,})")
    print(f"  est. tokens      {tokens:,.0f}")
    print(f"  est. cost        ${tokens / 1e6 * USD_PER_1M_TOKENS:.4f} "
          f"at ${USD_PER_1M_TOKENS}/1M ({settings.embedding_model})")
    print(f"  longest document {max((len(d) for d in docs), default=0):,} chars")
    await dispose_engine()
    return 0


async def main() -> int:
    args = parse_args()
    if args.estimate:
        return await estimate()

    embedder = GeminiEmbedder(concurrency=args.concurrency)
    print(f"[embed] {embedder.model} @ {embedder.dim}d, "
          f"concurrency {args.concurrency}")

    indexer = Indexer(embedder=embedder, batch_size=args.batch_size,
                      force=args.force,
                      include_delisted=not args.exclude_delisted)
    client = qi.make_client()
    try:
        async with session_scope() as session:
            if args.limit:
                # Smoke-test path: cap the work without touching the diff logic.
                original = indexer._to_embed

                async def capped(sess):
                    todo = await original(sess)
                    return todo[:args.limit]

                indexer._to_embed = capped
            await indexer.run(session, client, recreate=args.recreate)
        indexer.report()
    finally:
        await client.close()
        await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
