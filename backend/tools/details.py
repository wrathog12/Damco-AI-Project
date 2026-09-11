"""
get_scheme_details — the ~300-token summary the LLM speaks from.

Deliberately not the whole record: documents, the full application process and
the translations go to the card instead, where a screen can render them.
"""
from typing import Any

from services import schemes as svc
from services.resources import Resources


async def get_scheme_details(res: Resources, *,
                             scheme_id: str) -> dict[str, Any]:
    scheme = await svc.details(res, scheme_id)
    if scheme is None:
        return {"error": f"Scheme '{scheme_id}' not found."}
    return scheme
