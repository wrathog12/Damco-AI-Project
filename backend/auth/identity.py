"""
Resolving the caller of one HTTP request to a `User` row.

This is the single entry point every endpoint uses to answer "who is this?", and
it always succeeds. There is no unauthenticated branch that 401s, because an
anonymous caller is not a failed login — they are the default case, and the
product promise is that they get a conversation without being asked for anything.

The order of preference is authenticated first, anonymous second:

1. A valid access token (slice 2) identifies a phone-verified user with the larger
   allowance.
2. Otherwise the signed `bh_anon` cookie identifies an anonymous user.
3. Otherwise we mint a new anonymous identity and set the cookie on the way out.

Because step 3 writes a cookie, this is a dependency that takes `Response` rather
than a plain function — FastAPI lets a dependency mutate the outgoing response,
which is what keeps cookie management out of every endpoint body.

**On login, the anonymous row is abandoned, not merged.** See the note in
`models/identity.py`: merging two counters is the only thing it would buy, and a
returning caller on a second device makes it a genuine two-row merge with no
correct answer.
"""
from fastapi import Request, Response

from auth import anon, tokens
from config import settings
from models.identity import User
from services import auth as auth_service
from services import quota


def _access_user_id(request: Request) -> int | None:
    """The user id in this request's `Authorization` header, if it verifies."""
    return tokens.read_access(
        tokens.bearer_token(request.headers.get("authorization")))


async def current_user(request: Request, response: Response) -> User:
    """The caller's identity, minting an anonymous one if they have none.

    Never raises for a missing or broken credential. The only failure mode is the
    database being unreachable, which is not this function's problem to soften.
    """
    resources = request.app.state.resources

    # ── 1. authenticated? ───────────────────────────────────────────────
    # Checked *before* the cookie, which is the security-relevant part: a caller
    # who has logged in still has their old `bh_anon` cookie sitting in the
    # browser, and preferring it would quietly serve them the ten-turn anonymous
    # allowance they just signed in to escape.
    #
    # A token that verifies but names a row that no longer exists falls through to
    # anonymous rather than 401ing. That happens for up to
    # `access_token_ttl_minutes` after `DELETE /api/me`, and someone who has just
    # erased their account should get a working app, not an error.
    user_id = _access_user_id(request)
    if user_id is not None:
        user = await auth_service.user_by_id(resources, user_id)
        if user is not None:
            return user

    # ── 2. an anonymous identity we previously issued ───────────────────
    # Note that after a first login this cookie resolves to the *promoted* row —
    # the same row, now carrying a phone number — so the larger allowance applies
    # even on a request that forgot the bearer token. That is deliberate for the
    # quota, and it is why anything personal must check `token_verified` instead of
    # inferring authorisation from `user.phone_e164`.
    anon_id = anon.verify(request.cookies.get(settings.anon_cookie_name))

    # ── 3. first contact ────────────────────────────────────────────────
    if anon_id is None:
        anon_id, cookie_value = anon.mint()
        response.set_cookie(value=cookie_value, **anon.cookie_kwargs())

    return await quota.get_or_create_anon(resources, anon_id)


def token_verified(request: Request) -> bool:
    """Did this request actually present a valid access token?

    `user.phone_e164` answers "does this identity have an account", which is the
    right question for the *quota* and the wrong one for authorisation. The
    anonymous cookie of a promoted row still resolves to a row with a phone number
    on it, so anything that returns personal data — the masked number today,
    profiles and conversation history in slice 3 — must gate on this instead.

    It re-verifies the header rather than reading a flag `current_user` left
    behind. That is one extra signature check on the handful of endpoints that ask,
    and it buys independence from FastAPI's dependency resolution order — a
    security answer that is only correct when two dependencies happen to run in the
    right sequence is one refactor away from being wrong.
    """
    return _access_user_id(request) is not None
