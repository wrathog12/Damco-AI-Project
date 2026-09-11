"""
check_eligibility — compare a user profile against a scheme's requirements.

This stays a deterministic rules evaluation against typed Postgres columns — it
must never become a similarity score. A citizen may act on the answer, so it has
to be reproducible and explainable, which is what the `reasons[]` list is for.
"""
from typing import Any

from services import schemes as svc
from services.resources import Resources


async def check_eligibility(
    res: Resources,
    *,
    scheme_id: str,
    user_age: int | None = None,
    user_gender: str | None = None,
    user_state: str | None = None,
    user_income: int | None = None,
    user_occupation: str | None = None,
) -> dict[str, Any]:
    """`{eligible: bool, scheme_id, scheme_name, reasons: [str]}`."""
    verdict = await svc.evaluate_eligibility(
        res, scheme_id,
        user_age=user_age, user_gender=user_gender, user_state=user_state,
        user_income=user_income, user_occupation=user_occupation)

    if verdict is None:
        return {"error": f"Scheme '{scheme_id}' not found."}
    return verdict
