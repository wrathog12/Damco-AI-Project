"""
The caller's profile: consent, storage, and the memory injected into a session.

This is the slice-3 payoff. Once a profile exists the agent stops re-asking age,
state and income on every call, and `check_eligibility` can be called with a
scheme id and nothing else — the facts come from here.

## Consent comes first, and it is enforced in the service, not the endpoint

`save()` refuses unless `users.consent_version` matches `settings.consent_version`.
That check lives here rather than in `main.py` because the profile writer is not
only the endpoint: P5's background extractor is meant to pull "I'm 17, from Bihar"
out of a transcript and write it, and a consent check that only guarded the HTTP
route would be silently absent on the path that matters most. One rule, one place,
no path around it.

## Who gets a profile

Only an identity with a phone number. An anonymous caller cannot exercise erasure
— `DELETE /api/me` requires a verified token — so storing their income against a
cookie would create personal data whose subject has no way to reach it. They get
the entire product minus the memory, which is the trade the anonymous-first design
already makes everywhere else.

Note the asymmetry that follows, and it is intended: **writing** is gated on the
identity having a phone number, **reading** is gated on the request carrying a
verified token (`auth/identity.py::token_verified`). The two questions are
different. "Is there an account this belongs to, and can its owner erase it?" is
the right question for whether to persist. "May this request see it?" is the right
question for disclosure, and a cookie is not proof of a person.

## What a fact looks like

A small, closed set of scalars — the eligibility inputs and nothing else. There is
no free-text notes field, and adding one would be a mistake: it would become the
place a transcript's most sensitive sentence gets copied to, outside the retention
window that governs transcripts. **No Aadhaar number, ever**, and that is not a
validation rule to be relaxed later.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from auth import crypto
from config import settings
from models.identity import User
from models.profile import UserProfile
from services import conversations as conv_service
from services.auth import AuthError
from services.resources import Resources

# The closed set of facts, and the type each one has to be. Anything else in a
# patch is a 400 rather than a silently ignored key — a client that thinks it
# saved "pincode" should be told it did not.
_TYPES: dict[str, type] = {
    "age": int,
    "gender": str,
    "state": str,
    "income": int,
    "occupation": str,
    "caste": str,
    "disability": bool,
    "bpl_card": bool,
}
FIELDS = tuple(_TYPES)

# Bounds exist to catch nonsense, not to be clever: an age of 900 or an income of
# 10^12 is a typo or a bad unit, and storing it would produce eligibility verdicts
# that are confidently wrong.
_MAX_AGE = 120
_MAX_INCOME = 100_000_000
_MAX_TEXT = 64


@dataclass(frozen=True)
class Profile:
    """A decrypted profile. `facts` holds only keys the caller actually gave."""
    version: int
    facts: dict[str, Any]
    consent_version: str
    updated_at: datetime | None

    @property
    def known(self) -> tuple[str, ...]:
        return tuple(k for k in FIELDS if self.facts.get(k) is not None)


def _aad(user_id: int) -> str:
    """The ciphertext's binding. See auth/crypto.py — this is what stops a
    profile row being moved from one user to another and still opening."""
    return f"profile:{user_id}"


def storable(user: User) -> bool:
    """Whether a profile may be written for this identity at all."""
    return bool(user.phone_e164) and crypto.available()


def _require_storable(user: User) -> None:
    if not user.phone_e164:
        raise AuthError(403, "login_required",
                        "Sign in with your phone number to save your details.")
    if not crypto.available():
        # 503, not 500: the app is working, this feature is switched off, and the
        # operator's fix is a configuration change rather than a bug report.
        raise AuthError(503, "profiles_unavailable",
                        "Saving details is not available on this server.")


def consent_current(user: User) -> bool:
    return user.consent_version == settings.consent_version


async def record_consent(res: Resources, user: User) -> datetime:
    """Record that this caller agreed to the current privacy notice.

    Stored per version, so publishing a new notice invalidates the old agreement
    instead of inheriting it. Re-consenting to a version already agreed is
    allowed and simply refreshes the timestamp — it is what a client does after
    showing the notice again, and refusing it would make that a special case.
    """
    _require_storable(user)
    now = datetime.now(timezone.utc)
    async with res.session_factory() as session:
        await session.execute(
            update(User).where(User.id == user.id)
            .values(consent_version=settings.consent_version, consent_at=now))
        await session.commit()
    # Keep the detached row honest: it was loaded before this request and every
    # later consent check in the same request would otherwise still see the old
    # value and refuse the write this call just authorised.
    user.consent_version = settings.consent_version
    user.consent_at = now
    logger.info(f"[privacy] user {user.id} consented to "
                f"{settings.consent_version}")
    return now


# ── reads ───────────────────────────────────────────────
async def load(res: Resources, user: User) -> Profile | None:
    """The caller's profile, or None if there is none — or none we can open.

    A profile that fails to decrypt is reported as absent rather than as an
    error, because that is the behaviour the caller can live with: the agent asks
    for their state again. The alternative is a 500 on every request after a
    mis-set key. `auth/crypto.py` logs the real cause.
    """
    if not user.phone_e164:
        return None

    async with res.session_factory() as session:
        row = (await session.execute(
            select(UserProfile).where(UserProfile.user_id == user.id))
        ).scalars().first()

    if row is None:
        return None

    raw = crypto.decrypt(row.payload, aad=_aad(user.id))
    if raw is None:
        return None
    try:
        facts = json.loads(raw)
    except ValueError:                                    # pragma: no cover
        logger.error(f"[privacy] profile for user {user.id} decrypted to "
                     f"something that is not JSON")
        return None

    return Profile(version=row.version, facts=facts,
                   consent_version=row.consent_version,
                   updated_at=row.updated_at)


def eligibility_args(profile: Profile | None) -> dict[str, Any]:
    """Profile facts → `evaluate_eligibility` keyword arguments.

    The translation is here rather than in the tool so that the profile's field
    names and the eligibility evaluator's argument names can differ without one
    of them leaking into the other. Absent facts are simply omitted, which the
    evaluator already treats as "not stated, do not check".
    """
    if profile is None:
        return {}
    facts = profile.facts
    mapped = {
        "user_age": facts.get("age"),
        "user_gender": facts.get("gender"),
        "user_state": facts.get("state"),
        "user_income": facts.get("income"),
        "user_occupation": facts.get("occupation"),
        "user_caste": facts.get("caste"),
        "user_disability": facts.get("disability"),
        "user_bpl_card": facts.get("bpl_card"),
    }
    return {k: v for k, v in mapped.items() if v is not None}


def search_args(profile: Profile | None) -> dict[str, Any]:
    """The subset of a profile that `search_schemes` accepts as filters.

    Not applied automatically anywhere — filters are hard constraints
    (cross-cutting rule 3), and quietly narrowing a search by facts the caller
    did not mention in *this* question is how someone asking "what is there for
    my daughter" gets results filtered to their own gender. It exists for callers
    that decide to use it explicitly.
    """
    if profile is None:
        return {}
    facts = profile.facts
    mapped = {
        "state": facts.get("state"),
        "age": facts.get("age"),
        "gender": facts.get("gender"),
        "occupation": facts.get("occupation"),
        "income": facts.get("income"),
        "caste": facts.get("caste"),
        "disability": facts.get("disability"),
    }
    return {k: v for k, v in mapped.items() if v is not None}


# ── writes ──────────────────────────────────────────────
def _clean(patch: dict[str, Any]) -> dict[str, Any]:
    """Validate a patch, or raise the 400. `None` means "clear this field"."""
    unknown = sorted(set(patch) - set(_TYPES))
    if unknown:
        raise AuthError(400, "unknown_fields",
                        f"Not something this profile holds: "
                        f"{', '.join(unknown)}.")

    cleaned: dict[str, Any] = {}
    for key, value in patch.items():
        if value is None:
            cleaned[key] = None
            continue

        want = _TYPES[key]
        if want is bool:
            if not isinstance(value, bool):
                raise AuthError(400, "bad_value", f"'{key}' must be true or false.")
            cleaned[key] = value
        elif want is int:
            # `isinstance(True, int)` is True in Python, which would let a JSON
            # `true` through as an age of 1.
            if isinstance(value, bool) or not isinstance(value, int):
                raise AuthError(400, "bad_value", f"'{key}' must be a number.")
            cap = _MAX_AGE if key == "age" else _MAX_INCOME
            if not 0 <= value <= cap:
                raise AuthError(400, "bad_value",
                                f"'{key}' must be between 0 and {cap}.")
            cleaned[key] = value
        else:
            if not isinstance(value, str):
                raise AuthError(400, "bad_value", f"'{key}' must be text.")
            text = value.strip()
            if not text:
                # An empty string is how a form submits a cleared field, and
                # storing "" would make the evaluator check against nothing.
                cleaned[key] = None
            elif len(text) > _MAX_TEXT:
                raise AuthError(400, "bad_value",
                                f"'{key}' is too long (max {_MAX_TEXT}).")
            else:
                cleaned[key] = text
    return cleaned


async def save(res: Resources, user: User, patch: dict[str, Any]) -> Profile:
    """Merge `patch` into the caller's profile. Consent is required.

    A patch, not a replacement: the voice agent learns one fact at a time, and a
    PUT that replaced the whole object would mean a client which knows only the
    age erases the state. Passing `None` for a field clears it explicitly, so
    "forget my income" is expressible.
    """
    _require_storable(user)
    if not consent_current(user):
        # 403 rather than 401: the caller is authenticated, they simply have not
        # agreed to the notice that would let us hold this. A client's correct
        # response is to show the consent screen, not a login screen — hence a
        # distinct code, since matching on prose is how that breaks.
        raise AuthError(403, "consent_required",
                        "Please agree to how your details are used before saving "
                        "them.")

    cleaned = _clean(patch)
    existing = await load(res, user)
    facts = dict(existing.facts) if existing else {}
    for key, value in cleaned.items():
        if value is None:
            facts.pop(key, None)
        else:
            facts[key] = value

    sealed = crypto.encrypt(json.dumps(facts).encode(), aad=_aad(user.id))
    now = datetime.now(timezone.utc)

    async with res.session_factory() as session:
        # One statement, so two concurrent writers cannot both insert. `version`
        # is incremented by the database from whatever is there rather than from
        # what we read, which is what keeps the eligibility cache's invalidation
        # honest under a race — a lost update would leave a cached verdict
        # matching a version whose facts it was not computed from.
        stmt = pg_insert(UserProfile).values(
            user_id=user.id, payload=sealed, version=1,
            consent_version=settings.consent_version,
            created_at=now, updated_at=now,
        )
        version = (await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[UserProfile.user_id],
                set_={
                    "payload": sealed,
                    "version": UserProfile.version + 1,
                    "consent_version": settings.consent_version,
                    "updated_at": now,
                },
            ).returning(UserProfile.version))).scalar_one()
        await session.commit()

    # The cached verdicts were computed from the facts that just changed, and
    # their `reasons` quote them. Dropped here rather than in the endpoint for the
    # same reason the consent check is here: the endpoint is not the only writer.
    from services import eligibility as eligibility_service
    await eligibility_service.invalidate(res, user)

    # Only the field *names*, never the values: this line is the difference
    # between a useful log and a log that has to be treated as personal data.
    logger.info(f"[privacy] profile saved for user {user.id} v{version} "
                f"fields={sorted(facts)}")
    return Profile(version=version, facts=facts,
                   consent_version=settings.consent_version, updated_at=now)


async def erase(res: Resources, user: User) -> bool:
    """Delete the profile, keeping the account. Returns whether there was one.

    A data right short of closing the account: someone who wants the agent to
    stop knowing their income should not have to lose their phone number and
    their turn allowance to get it. The cached eligibility verdicts go with it,
    because their `reasons` quote the facts being erased.
    """
    async with res.session_factory() as session:
        result = await session.execute(
            delete(UserProfile).where(UserProfile.user_id == user.id))
        await session.commit()
    removed = bool(result.rowcount)
    if removed:
        # Imported here rather than at module scope: `services/eligibility.py`
        # reads profiles, so importing it at the top would be a cycle.
        from services import eligibility as eligibility_service
        await eligibility_service.invalidate(res, user)
        logger.info(f"[privacy] profile erased for user {user.id}")
    return removed


# ── the memory a session opens with ─────────────────────
async def session_context(res: Resources, user: User) -> str | None:
    """The one system message a new session starts knowing, or None.

    This is tiers L2 and L3 of the plan's memory model, and deliberately **not**
    L1: the transcript of the last call is persisted, but it is not replayed into
    the context. Restoring raw messages would let the model resume a week-old
    thread as though it were still in progress, and it would put a stranger's
    worth of tokens in front of every turn. A recap of facts plus which schemes
    were discussed is what actually makes the second call better than the first.

    Returns prose because the consumer is a language model. The wording lives in
    `voice/prompts.py`, which is the only place prompt text is allowed to live.
    """
    if not user.phone_e164:
        return None

    profile = await load(res, user)
    recent = await conv_service.recent_schemes(res, user)
    if profile is None and not recent:
        return None

    from voice.prompts import profile_notice
    return profile_notice(profile.facts if profile else {}, recent)
