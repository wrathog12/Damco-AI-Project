"""
check_eligibility — compare a user profile against a scheme's requirements.

This stays a deterministic rules evaluation against typed Postgres columns — it
must never become a similarity score. A citizen may act on the answer, so it has
to be reproducible and explainable, which is what the `reasons[]` list is for.

Since P3 slice 3 it can also be called with **only** a `scheme_id`: anything the
model does not supply is filled from the caller's stored profile. That is the
payoff of the profiles slice, and the precedence is deliberate — an argument the
model passed wins over a stored fact, because the model heard "actually I'm 62"
this turn and the profile is what someone said months ago.
"""
from typing import Any

from services import schemes as svc
from services.resources import Resources


async def check_eligibility(
    res: Resources,
    *,
    scheme_id: str,
    profile_facts: dict[str, Any] | None = None,
    user_age: int | None = None,
    user_gender: str | None = None,
    user_state: str | None = None,
    user_income: int | None = None,
    user_occupation: str | None = None,
    user_caste: str | None = None,
    user_disability: bool | None = None,
    user_bpl_card: bool | None = None,
) -> dict[str, Any]:
    """`{eligible: bool, scheme_id, scheme_name, reasons: [str], used_profile}`.

    `profile_facts` is filled in by the registry from the caller's stored profile,
    never by the model — it is not in the tool schema. Keeping it a parameter here
    rather than merging in the registry means this function remains the single
    place that decides which value wins.
    """
    stated = {
        "user_age": user_age,
        "user_gender": user_gender,
        "user_state": user_state,
        "user_income": user_income,
        "user_occupation": user_occupation,
        "user_caste": user_caste,
        "user_disability": user_disability,
        "user_bpl_card": user_bpl_card,
    }
    facts = dict(profile_facts or {})
    from_profile = [k for k, v in facts.items() if stated.get(k) is None]
    facts.update({k: v for k, v in stated.items() if v is not None})

    verdict = await svc.evaluate_eligibility(res, scheme_id, **facts)
    if verdict is None:
        return {"error": f"Scheme '{scheme_id}' not found."}

    if not facts:
        # Nothing known at all: every check is skipped, so `eligible: true` would
        # be true only in the sense that nothing contradicted it. Saying so is the
        # difference between the model asking one question and it telling someone
        # they qualify for something they do not.
        verdict["reasons"] = verdict["reasons"] + [
            "No details known yet — ask the user for their age, state and income "
            "before saying whether they qualify."
        ]
        verdict["eligible"] = False
        verdict["needs_details"] = True

    # Which fields came from memory rather than from this conversation. The model
    # is told so it can confirm the ones that matter before an application, per
    # the profile notice in voice/prompts.py.
    if from_profile:
        verdict["used_profile"] = sorted(
            k.removeprefix("user_") for k in from_profile)
    return verdict
