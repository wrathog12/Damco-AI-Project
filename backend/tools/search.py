"""
search_schemes — hybrid retrieval over Postgres + Qdrant.

`query` is the semantic half and the demographic arguments are hard filters, not
search terms: a Bihar query can never return a Maharashtra scheme (cross-cutting
rule 3). A call with filters and no `query` still ranks — see
`services.schemes._implied_query`.

The tool returns a plain dict; `tools.registry` is what serialises it for the
LLM. That keeps the shape testable without going through a wire format.
"""
from typing import Any

from services import schemes as svc
from services.resources import Resources

RESULT_LIMIT = 5


async def search_schemes(
    res: Resources,
    *,
    query: str | None = None,
    state: str | None = None,
    category: str | None = None,
    age: int | None = None,
    gender: str | None = None,
    occupation: str | None = None,
    income: int | None = None,
    caste: str | None = None,
    disability: bool | None = None,
) -> dict[str, Any]:
    results = await svc.search(
        res, query=query, state=state, category=category, age=age,
        gender=gender, occupation=occupation, income=income, caste=caste,
        disability=disability, limit=RESULT_LIMIT)

    if not results:
        return {"matches": 0,
                "message": "No schemes found matching the criteria."}
    return {"matches": len(results), "schemes": results}
