"""
P1 retrieval verification gates.

These are the checks the migration plan names, turned into something runnable:

  1. A Hindi query, a Hinglish query and an exact scheme-name query each return
     a sensible scheme in the top 3.
  2. **Regression gate:** a state-filtered query returns *zero* out-of-state
     results. This is cross-cutting rule 3 and the property v1 got right by
     accident (exact string filtering); hybrid search could silently lose it,
     because a vector will happily rank a Maharashtra scheme against "Bihar".
  3. The NULL-means-unspecified invariant: a scheme with no stated age limit must
     survive an age filter.

Exits non-zero if a gate fails, so it can gate a deploy.

    python backend/scripts/verify_retrieval.py
"""
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import func, select                       # noqa: E402

from db.session import dispose_engine, session_scope      # noqa: E402
from embeddings.gemini import GeminiEmbedder              # noqa: E402
from models import Scheme                                 # noqa: E402
from search import qdrant_index as qi                     # noqa: E402
from search.sparse import encode_query                    # noqa: E402


async def search(embedder, client, collection, query, *, limit=5, **filters):
    dense = await embedder.embed_query(query)
    sparse = encode_query(query)
    return await qi.hybrid_search(
        client, collection, dense=dense,
        sparse_indices=sparse[0], sparse_values=sparse[1],
        query_filter=qi.eligibility_filter(**filters), limit=limit)


async def main() -> int:
    embedder = GeminiEmbedder()
    client = qi.make_client()
    failures: list[str] = []

    try:
        collection = "schemes"
        info = await client.get_collection(collection)
        print(f"collection '{collection}': {info.points_count} points\n")

        # A real name from the corpus, so the exact-name gate is not synthetic.
        async with session_scope() as session:
            target = (await session.execute(
                select(Scheme.scheme_name, Scheme.state)
                .where(Scheme.scheme_name.ilike("%scholarship%"))
                .order_by(func.length(Scheme.scheme_name))
                .limit(1)
            )).first()
            # Must exclude "All": it is the most common `state` value (every
            # nationwide scheme), and filtering nationwide schemes by "All"
            # trivially leaks nothing, so the gate would pass without testing
            # anything. We need a real state with real neighbours to leak from.
            states = [s for s, in (await session.execute(
                select(Scheme.state)
                .where(Scheme.state.is_not(None), Scheme.state != "All")
                .group_by(Scheme.state)
                .order_by(func.count().desc()).limit(1))).all()]
        busiest_state = states[0] if states else "Bihar"

        # ── gate 1: semantic queries across languages ───────────────────
        print("-- semantic queries " + "-" * 40)
        for label, query in (
            ("english ", "scholarship for poor students"),
            ("hindi   ", "गरीब छात्रों के लिए छात्रवृत्ति"),
            ("hinglish", "garib students ke liye scholarship"),
            ("bengali ", "দরিদ্র ছাত্রদের জন্য বৃত্তি"),
        ):
            points = await search(embedder, client, collection, query, limit=3)
            if not points:
                failures.append(f"{label.strip()} query returned nothing")
            print(f"\n[{label}] {query}")
            for p in points:
                print(f"    {p.score:.4f}  {p.payload['scheme_name'][:60]}")

        # ── gate 2: exact scheme name ──────────────────────────────────
        if target:
            name = target[0]
            print("\n-- exact name " + "-" * 46)
            print(f"looking for: {name[:70]}")
            points = await search(embedder, client, collection, name, limit=3)
            names = [p.payload["scheme_name"] for p in points]
            hit = any(n == name for n in names)
            print(f"    top-3 contains exact match: {hit}")
            for n in names:
                print(f"      {n[:66]}")
            if not hit:
                failures.append(f"exact-name query missed its own scheme: {name[:50]}")

        # ── gate 3: the state-filter regression gate ────────────────────
        print("\n-- state filter (regression gate) " + "-" * 26)
        points = await search(embedder, client, collection,
                              "scholarship for students", limit=10,
                              state=busiest_state)
        leaks = [p.payload["scheme_name"] for p in points
                 if p.payload.get("states")
                 and busiest_state not in p.payload["states"]]
        print(f"    state={busiest_state}: {len(points)} results, "
              f"{len(leaks)} out-of-state")
        if leaks:
            failures.append(f"state filter leaked {len(leaks)} out-of-state: {leaks[:3]}")
        for p in points[:5]:
            print(f"      {p.payload['scheme_name'][:52]} "
                  f"| {p.payload.get('states') or 'All India'}")

        # ── gate 4: NULL means unspecified, never exclude ───────────────
        print("\n-- NULL eligibility invariant " + "-" * 30)
        async with session_scope() as session:
            no_age = (await session.execute(
                select(func.count()).select_from(Scheme)
                .where(Scheme.age_min.is_(None), Scheme.age_max.is_(None)))).scalar()
        unfiltered = await search(embedder, client, collection,
                                  "government help", limit=50)
        aged = await search(embedder, client, collection,
                            "government help", limit=50, age=30)
        no_age_survivors = [p for p in aged
                            if p.payload.get("age_min") is None
                            and p.payload.get("age_max") is None]
        print(f"    schemes with no stated age in Postgres: {no_age}")
        print(f"    results unfiltered={len(unfiltered)} age=30 -> {len(aged)}")
        print(f"    of those, age-unspecified survivors: {len(no_age_survivors)}")
        if no_age and not no_age_survivors:
            failures.append("age filter excluded every age-unspecified scheme — "
                            "the do-not-exclude invariant is broken")

        print("\n" + "=" * 60)
        if failures:
            print(f"FAILED {len(failures)} gate(s):")
            for f in failures:
                print(f"  - {f}")
        else:
            print("All retrieval gates passed.")
        print(f"api calls: {embedder.stats['requests']}")
        return 1 if failures else 0
    finally:
        await client.close()
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
