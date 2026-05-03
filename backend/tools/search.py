"""
search_schemes tool — discover schemes matching user intent.
"""
import json
from knowledge.index import filter_schemes


def search_schemes(
    state: str | None = None,
    category: str | None = None,
    age: int | None = None,
    gender: str | None = None,
    occupation: str | None = None,
    income: int | None = None,
    caste: str | None = None,
    disability: bool | None = None,
) -> str:
    """
    Search the knowledge base for schemes matching the given criteria.
    Returns a JSON string of matching scheme summaries.
    """
    results = filter_schemes(
        state=state,
        category=category,
        age=age,
        gender=gender,
        occupation=occupation,
        income=income,
        caste=caste,
        disability=disability,
        limit=5,
    )

    if not results:
        return json.dumps({"matches": 0, "message": "No schemes found matching the criteria."})

    return json.dumps({"matches": len(results), "schemes": results}, ensure_ascii=False)
