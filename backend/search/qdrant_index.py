"""
Qdrant hybrid index: dense (Gemini) + sparse (BM25).

Why both, from the plan: pure dense retrieval fumbles exact scheme names —
"Kanyashree", "PM Vishwakarma" — which is precisely how callers refer to
schemes. BM25 catches the literal token; the dense vector catches "money for my
daughter's school fees". Fused with RRF.

**The hard filter goes on every prefetch, not on the fusion query.** Qdrant
applies prefetch filters *before* fusion, so a filter placed only at the top
level would let an out-of-state scheme into a candidate list and then rank it.
That is the mechanism behind cross-cutting rule 3 — a Bihar query must never
return a Maharashtra scheme — so `_prefetch` builds both branches from one
filter object rather than letting callers pass them separately.

NULL handling is the other load-bearing detail. A NULL eligibility value means
"unspecified, do not exclude". Qdrant's `must` on a missing field would drop the
row, so age/income constraints are expressed as
`IsNull(field) OR range-contains-user` — see `eligibility_filter`.
"""
from typing import Any, Iterable, Sequence

from qdrant_client import AsyncQdrantClient, models

from config import settings

DENSE = "dense"
SPARSE = "sparse"

# Fields we filter or facet on. Unindexed payload filtering in Qdrant works but
# degrades to a full scan, which defeats filterable-HNSW.
_KEYWORD_INDEXES = ("state", "states", "level", "category", "categories",
                    "subcategories", "tags", "gender", "caste",
                    "state_residence", "target_beneficiaries")
_INT_INDEXES = ("age_min", "age_max")
_FLOAT_INDEXES = ("income_max",)
_BOOL_INDEXES = ("disability", "bpl_card", "student", "minority", "delisted")
# `occupation` is one free-text string per scheme ("Farmer, Agricultural
# Labourer"), so it needs token matching rather than an exact keyword match —
# hence a full-text index. `MatchText` errors outright without one.
_TEXT_INDEXES = ("occupation",)


def make_client() -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=60,
    )


async def ensure_collection(
    client: AsyncQdrantClient,
    *,
    dim: int,
    collection: str | None = None,
    recreate: bool = False,
) -> str:
    """Create the collection and payload indexes if absent. Idempotent."""
    name = collection or settings.qdrant_collection

    if recreate and await client.collection_exists(name):
        await client.delete_collection(name)

    if not await client.collection_exists(name):
        await client.create_collection(
            collection_name=name,
            vectors_config={
                DENSE: models.VectorParams(size=dim,
                                           distance=models.Distance.COSINE),
            },
            sparse_vectors_config={
                # IDF is computed server-side; fastembed emits raw term
                # frequencies, so without this modifier BM25 scoring is wrong.
                SPARSE: models.SparseVectorParams(
                    modifier=models.Modifier.IDF),
            },
        )

    for field in _KEYWORD_INDEXES:
        await _ensure_index(client, name, field, models.PayloadSchemaType.KEYWORD)
    for field in _INT_INDEXES:
        await _ensure_index(client, name, field, models.PayloadSchemaType.INTEGER)
    for field in _FLOAT_INDEXES:
        await _ensure_index(client, name, field, models.PayloadSchemaType.FLOAT)
    for field in _BOOL_INDEXES:
        await _ensure_index(client, name, field, models.PayloadSchemaType.BOOL)
    for field in _TEXT_INDEXES:
        await _ensure_index(client, name, field, models.TextIndexParams(
            type=models.TextIndexType.TEXT,
            tokenizer=models.TokenizerType.WORD,
            lowercase=True, min_token_len=2, max_token_len=30))
    return name


async def _ensure_index(client: AsyncQdrantClient, collection: str,
                        field: str, schema: Any) -> None:
    try:
        await client.create_payload_index(collection, field, field_schema=schema)
    except Exception:                                  # noqa: BLE001
        # Already present. Qdrant has no create-if-absent for payload indexes and
        # the error type varies by transport, so this is the sanctioned idiom.
        pass


# ── filters ─────────────────────────────────────────────────────────────
def _null_or(field: str, *conditions: models.Condition) -> models.Filter:
    """`field IS NULL OR <any condition>` — the do-not-exclude invariant.

    Every eligibility filter must be expressible this way. A scheme that never
    stated an age limit has to survive an age filter, because telling a citizen
    they are ineligible on the strength of missing data is the one failure mode
    this system must not have.
    """
    return models.Filter(should=[
        models.IsNullCondition(is_null=models.PayloadField(key=field)),
        *conditions,
    ])


def eligibility_filter(
    *,
    state: str | None = None,
    age: int | None = None,
    income: float | None = None,
    gender: str | None = None,
    caste: str | None = None,
    category: str | None = None,
    level: str | None = None,
    occupation: str | None = None,
    disability: bool | None = None,
    include_delisted: bool = False,
) -> models.Filter | None:
    """Hard constraints only. Ranking happens after this, never instead of it."""
    must: list[models.Condition] = []

    if not include_delisted:
        must.append(models.FieldCondition(key="delisted",
                                          match=models.MatchValue(value=False)))

    if state:
        # A nationwide scheme has an empty `states`, so it can only be matched
        # via the null branch — hence `_null_or` rather than a bare match.
        must.append(_null_or("states", models.FieldCondition(
            key="states", match=models.MatchAny(any=[state, "All"]))))

    if category:
        must.append(models.FieldCondition(
            key="categories", match=models.MatchAny(any=[category])))

    if level:
        # Always populated (Central | State), so no null branch. This is how
        # "Central Government" is expressed: central schemes carry state="All"
        # and no `states`, so they cannot be selected by a state filter.
        must.append(models.FieldCondition(key="level",
                                          match=models.MatchValue(value=level)))

    if age is not None:
        # age_min <= age <= age_max, with either bound possibly unstated.
        must.append(_null_or("age_min", models.FieldCondition(
            key="age_min", range=models.Range(lte=age))))
        must.append(_null_or("age_max", models.FieldCondition(
            key="age_max", range=models.Range(gte=age))))

    if income is not None:
        must.append(_null_or("income_max", models.FieldCondition(
            key="income_max", range=models.Range(gte=income))))

    if gender:
        must.append(_null_or("gender", models.FieldCondition(
            key="gender", match=models.MatchAny(any=[gender, "All"]))))

    if caste:
        must.append(_null_or("caste", models.FieldCondition(
            key="caste", match=models.MatchAny(any=[caste, "All"]))))

    if occupation:
        # v1 matched occupation fuzzily (`knowledge/index.py`): substring against
        # the scheme's occupation string, falling back to its tags, and a scheme
        # that never named an occupation was never excluded. Same three branches
        # here — MatchText so "Farmer" hits "Farmer, Agricultural Labourer".
        must.append(_null_or(
            "occupation",
            models.FieldCondition(key="occupation",
                                  match=models.MatchText(text=occupation)),
            models.FieldCondition(key="tags",
                                  match=models.MatchAny(any=[occupation]))))

    if disability is not None:
        # Symmetric on purpose: a disability-only scheme is excluded for a caller
        # who said they have none, exactly as v1 did.
        must.append(_null_or("disability", models.FieldCondition(
            key="disability", match=models.MatchValue(value=disability))))

    return models.Filter(must=must) if must else None


# ── write path ──────────────────────────────────────────────────────────
def to_point(
    *,
    point_id: int,
    dense: Sequence[float],
    sparse_indices: Sequence[int],
    sparse_values: Sequence[float],
    payload: dict[str, Any],
) -> models.PointStruct:
    return models.PointStruct(
        id=point_id,
        vector={
            DENSE: list(dense),
            SPARSE: models.SparseVector(indices=list(sparse_indices),
                                        values=list(sparse_values)),
        },
        payload=payload,
    )


async def upsert_points(client: AsyncQdrantClient, collection: str,
                        points: Iterable[models.PointStruct]) -> None:
    batch = list(points)
    if batch:
        await client.upsert(collection_name=collection, points=batch, wait=True)


# ── read path ───────────────────────────────────────────────────────────
async def hybrid_search(
    client: AsyncQdrantClient,
    collection: str,
    *,
    dense: Sequence[float],
    sparse_indices: Sequence[int],
    sparse_values: Sequence[float],
    query_filter: models.Filter | None,
    limit: int = 5,
    candidates: int = 40,
) -> list[models.ScoredPoint]:
    """RRF fusion over a dense and a sparse prefetch sharing one filter."""
    result = await client.query_points(
        collection_name=collection,
        prefetch=[
            models.Prefetch(query=models.SparseVector(
                                indices=list(sparse_indices),
                                values=list(sparse_values)),
                            using=SPARSE, filter=query_filter, limit=candidates),
            models.Prefetch(query=list(dense),
                            using=DENSE, filter=query_filter, limit=candidates),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
        with_payload=True,
    )
    return result.points
