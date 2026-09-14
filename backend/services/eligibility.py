"""
Cached eligibility verdicts, keyed to the profile version they were computed from.

`services/schemes.py::evaluate_scheme` is the authority and this module never
second-guesses it. What it adds is the ability to answer "which of the 1,786
schemes is this caller eligible for?" without running the rules 1,786 times on
every request — and to do it without ever showing a verdict computed from facts
the caller has since corrected.

## The cache is invalidated by a version number, not a timestamp

`user_profiles.version` is bumped by the database on every profile write, and a
cached row records the version it was computed from. A row whose `profile_version`
is behind is simply not used. That is stricter than comparing `evaluated_at`
against `profiles.updated_at` — no clock skew to reason about, no window in which
two writes inside the same timestamp resolution hide one another — and it is why
`profiles.save` takes the new version back from `RETURNING` rather than computing
it in Python.

The corpus can change under a cached row too, and that is not versioned here. It
is accepted: ingestion is a scheduled batch job that runs far less often than a
profile changes, `evaluate_all` recomputes any scheme it has no fresh row for, and
the reasons are only ever *displayed* — `check_eligibility` on a specific scheme
always re-evaluates against the live record, because that is the answer a citizen
may act on.

## What is encrypted and what is not

`eligible` is stored in the clear: the count is the entire point of the table and a
boolean against one scheme reveals very little. `reasons` is sealed, because the
strings quote the inputs back — "Income ₹48,000: ✓ within limit ₹1,00,000" *is* the
caller's income written out in prose, and storing that unencrypted next to an
encrypted profile would make the encryption decorative.
"""
import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from auth import crypto
from models.identity import User
from models.profile import EligibilityEvaluation
from models.scheme import Scheme
from services import profiles as profile_service
from services import schemes as scheme_service
from services.profiles import Profile
from services.resources import Resources


def _aad(user_id: int) -> str:
    return f"eligibility:{user_id}"


async def invalidate(res: Resources, user: User) -> int:
    """Drop every cached verdict for this caller. Returns how many went.

    Called whenever the inputs change — a profile save, a profile erasure — and
    deliberately a delete rather than a flag: a stale row nobody will ever trust
    again is personal data being retained for no purpose.
    """
    async with res.session_factory() as session:
        result = await session.execute(
            delete(EligibilityEvaluation)
            .where(EligibilityEvaluation.user_id == user.id))
        await session.commit()
    return result.rowcount or 0


async def remember(res: Resources, user: User, verdict: dict[str, Any], *,
                   profile_version: int) -> None:
    """Cache one verdict. Silent on failure — this is never the caller's answer.

    A tool handler that returned an error because a *cache* write failed would
    turn a working eligibility answer into a broken turn, which is a strictly
    worse outcome than an uncached one.
    """
    if not crypto.available():
        return
    try:
        sealed = crypto.encrypt(
            json.dumps(verdict.get("reasons") or [],
                       ensure_ascii=False).encode(), aad=_aad(user.id))
        async with res.session_factory() as session:
            stmt = pg_insert(EligibilityEvaluation).values(
                user_id=user.id, scheme_id=verdict["scheme_id"],
                eligible=bool(verdict["eligible"]), reasons=sealed,
                profile_version=profile_version,
                evaluated_at=datetime.now(timezone.utc))
            await session.execute(stmt.on_conflict_do_update(
                index_elements=[EligibilityEvaluation.user_id,
                                EligibilityEvaluation.scheme_id],
                set_={"eligible": stmt.excluded.eligible,
                      "reasons": stmt.excluded.reasons,
                      "profile_version": stmt.excluded.profile_version,
                      "evaluated_at": stmt.excluded.evaluated_at}))
            await session.commit()
    except Exception as exc:                                    # noqa: BLE001
        logger.warning(f"[eligibility] could not cache {verdict.get('scheme_id')} "
                       f"for user {user.id}: {type(exc).__name__}: {exc}")


async def evaluate_all(res: Resources, user: User, *,
                       eligible_only: bool = True,
                       limit: int = 50) -> dict[str, Any]:
    """Score the whole corpus against this caller's profile.

    This is what makes "you are eligible for 12 schemes you have not looked at"
    possible. **One** `SELECT` over the live corpus and the rules run in Python —
    the plan's original "pre-compute per user on a schedule" was rejected because
    it does work for callers who never come back and is stale for the ones who do.
    On demand plus the cache is the same answer, computed when someone asks.

    A profile with no facts returns nothing rather than everything: every scheme
    is trivially "eligible" when nothing is known about the caller, and presenting
    1,786 of them as a personalised result would be a lie told at length.
    """
    profile = await profile_service.load(res, user)
    if profile is None or not profile.known:
        return {"total": 0, "eligible_count": 0, "schemes": [],
                "profile_fields": []}

    facts = profile_service.eligibility_args(profile)

    async with res.session_factory() as session:
        # `delisted_at IS NULL` in the query rather than relying on the rule
        # inside `evaluate_scheme`: there is no point scoring a scheme whose only
        # possible verdict is "no longer listed".
        rows = (await session.execute(
            select(Scheme).where(Scheme.delisted_at.is_(None)))).scalars().all()

    verdicts: list[dict[str, Any]] = []
    for scheme in rows:
        verdict = scheme_service.evaluate_scheme(scheme, **facts)
        if verdict["eligible"] or not eligible_only:
            verdicts.append(verdict)

    eligible_count = sum(1 for v in verdicts if v["eligible"])

    # Cache the eligible ones only. Caching all 1,786 would be ~1,786 rows per
    # user of mostly "not eligible, here is why" — a large, growing, encrypted
    # table whose only reader is a count we just computed in a few milliseconds.
    await _remember_many(res, user, [v for v in verdicts if v["eligible"]],
                         profile_version=profile.version)

    return {
        "total": len(rows),
        "eligible_count": eligible_count,
        "profile_fields": list(profile.known),
        "schemes": [{"scheme_id": v["scheme_id"],
                     "scheme_name": v["scheme_name"],
                     "reasons": v["reasons"]}
                    for v in verdicts[:limit]],
    }


async def _remember_many(res: Resources, user: User,
                         verdicts: list[dict[str, Any]], *,
                         profile_version: int) -> None:
    """`remember` for a batch, in one statement instead of one per scheme."""
    if not verdicts or not crypto.available():
        return
    now = datetime.now(timezone.utc)
    try:
        payload = [{
            "user_id": user.id,
            "scheme_id": v["scheme_id"],
            "eligible": bool(v["eligible"]),
            "reasons": crypto.encrypt(
                json.dumps(v.get("reasons") or [], ensure_ascii=False).encode(),
                aad=_aad(user.id)),
            "profile_version": profile_version,
            "evaluated_at": now,
        } for v in verdicts]

        async with res.session_factory() as session:
            stmt = pg_insert(EligibilityEvaluation).values(payload)
            await session.execute(stmt.on_conflict_do_update(
                index_elements=[EligibilityEvaluation.user_id,
                                EligibilityEvaluation.scheme_id],
                set_={"eligible": stmt.excluded.eligible,
                      "reasons": stmt.excluded.reasons,
                      "profile_version": stmt.excluded.profile_version,
                      "evaluated_at": stmt.excluded.evaluated_at}))
            await session.commit()
    except Exception as exc:                                    # noqa: BLE001
        logger.warning(f"[eligibility] could not cache {len(verdicts)} verdicts "
                       f"for user {user.id}: {type(exc).__name__}: {exc}")


async def cached_count(res: Resources, user: User,
                       profile: Profile | None = None) -> int | None:
    """How many schemes this caller is cached as eligible for, or None if unknown.

    Cheap enough to sit on `GET /api/me` — a count over a composite primary key —
    and returning None rather than 0 when nothing is cached matters: "we have not
    worked it out yet" and "you qualify for nothing" are very different things to
    show someone.
    """
    if profile is None:
        profile = await profile_service.load(res, user)
    if profile is None:
        return None

    async with res.session_factory() as session:
        rows = (await session.execute(
            select(EligibilityEvaluation.scheme_id)
            .where(EligibilityEvaluation.user_id == user.id,
                   EligibilityEvaluation.eligible.is_(True),
                   EligibilityEvaluation.profile_version == profile.version))
        ).scalars().all()
    return len(rows) or None
