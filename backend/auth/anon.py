"""
The anonymous identity cookie: mint, sign, verify.

A first-time caller has no account and we ask for nothing, but the quota still has
to attach to *somebody* — so the server mints a uuid, signs it, and sets it as an
httpOnly cookie. That value is the caller's identity for as long as they keep it.

## Why signed rather than a bare uuid

A bare uuid in a cookie is client-controlled: anyone can send a fresh one per
request and never run out of free turns. Signing does not stop someone *deleting*
the cookie to get a new identity — nothing can — but it does mean every id we
count against was issued by us, so the quota table cannot be filled with invented
identities and the row count stays proportional to real callers.

## Why HMAC and not a JWT

There are no claims here. The payload is one uuid, there is nothing to expire
server-side (the cookie's own `Max-Age` does that), and nothing needs to be read
by another service. `hmac` from the standard library is the whole requirement, so
it is also the whole dependency. Slice 2's access tokens are a different problem
and will use a real JWT library.

## What this deliberately does not do

**No IP address, no fingerprint.** Both make a stable identifier out of something
the DPDP Act then obliges us to justify, disclose and expire — for a bucket of ten
turns. Cookie-clearing is an accepted bypass: the free allowance is small enough
that evading it is more effort than logging in, which is the outcome we want
anyway.
"""
import hmac
import uuid
from hashlib import sha256

from loguru import logger

from config import settings

# `.` cannot appear in either half — urlsafe base64 uses `-` and `_`, and a uuid
# uses hex and `-` — so a single split is unambiguous.
_SEP = "."


def _sign(anon_id: str) -> str:
    """The tag for one id. Truncated to 32 chars: this authenticates a random
    uuid, not a secret, so 128 bits of tag is already far past the point where
    forging is the easy attack."""
    mac = hmac.new(settings.jwt_secret.encode(), anon_id.encode(), sha256)
    return mac.hexdigest()[:32]


def mint() -> tuple[str, str]:
    """A brand-new identity. Returns `(anon_id, cookie_value)`.

    The id and the cookie are returned separately because they go to different
    places: the id is what the database row is keyed on, the cookie is what the
    browser stores. Nothing should ever parse the id back out of the cookie except
    `verify`.
    """
    anon_id = str(uuid.uuid4())
    return anon_id, f"{anon_id}{_SEP}{_sign(anon_id)}"


def verify(cookie_value: str | None) -> str | None:
    """The `anon_id` inside a cookie we signed, or None.

    None covers every failure identically — absent, malformed, wrong signature —
    because the caller's response to all three is the same: mint a new identity and
    carry on. There is no case where a bad cookie should produce an error rather
    than a fresh start; a caller whose cookie broke is still a caller.
    """
    if not cookie_value:
        return None

    anon_id, _, tag = cookie_value.partition(_SEP)
    if not anon_id or not tag:
        return None

    # `compare_digest`, not `==`: signature comparison leaks its result through
    # timing otherwise. Cheap here, and the habit is what matters.
    if not hmac.compare_digest(tag, _sign(anon_id)):
        # Worth a line, because a burst of these means either the secret rotated
        # (everyone's quota just reset) or someone is probing. Never log the value.
        logger.debug("[anon] rejected a cookie with a bad signature")
        return None

    # A signed value that is not a uuid can only come from a secret we also used
    # to sign something else. Treat it as untrusted rather than storing it.
    try:
        uuid.UUID(anon_id)
    except ValueError:
        return None

    return anon_id


def cookie_kwargs() -> dict:
    """Arguments for `Response.set_cookie`, in one place.

    `httponly` keeps it away from JavaScript, so an XSS in the frontend cannot
    read or transplant an identity. `samesite="lax"` is what allows the cookie to
    ride along on the top-level navigation that starts a session while still
    blocking it on cross-site subrequests.

    `secure` is **not** set here: it would stop the cookie working on
    `http://localhost`, which is the whole development story. It must be turned on
    for any deployment that is not localhost — the deployment checklist item, not
    a code default that silently breaks local work.
    """
    return {
        "key": settings.anon_cookie_name,
        "httponly": True,
        "samesite": "lax",
        "max_age": settings.anon_cookie_ttl_days * 24 * 60 * 60,
        "path": "/",
    }
