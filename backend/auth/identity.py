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

from auth import anon
from config import settings
from models.identity import User
from services import quota


async def current_user(request: Request, response: Response) -> User:
    """The caller's identity, minting an anonymous one if they have none.

    Never raises for a missing or broken credential. The only failure mode is the
    database being unreachable, which is not this function's problem to soften.
    """
    resources = request.app.state.resources

    # ── 1. authenticated? (slice 2 fills this in) ───────────────────────
    # Deliberately left as an explicit no-op rather than omitted: the ordering is
    # the security-relevant part, and a reader needs to see that a bearer token is
    # checked *before* the cookie so a logged-in caller on a device that still has
    # an anonymous cookie gets their real allowance.

    # ── 2. an anonymous identity we previously issued ───────────────────
    anon_id = anon.verify(request.cookies.get(settings.anon_cookie_name))

    # ── 3. first contact ────────────────────────────────────────────────
    if anon_id is None:
        anon_id, cookie_value = anon.mint()
        response.set_cookie(value=cookie_value, **anon.cookie_kwargs())

    return await quota.get_or_create_anon(resources, anon_id)
