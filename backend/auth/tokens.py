"""
Access tokens and refresh tokens — mint, verify, and the cookie the latter rides in.

Two credentials, deliberately different in kind:

* **The access token is a JWT.** It is checked by signature alone on every
  request, with no database read, because `current_user` runs in front of `/chat`
  and `/api/offer` and a Postgres round-trip per request buys nothing there. The
  price of that is that it cannot be revoked, which is why it lives fifteen
  minutes.
* **The refresh token is opaque and stored.** It is presented rarely, so a
  database read is free, and being in the database is what makes revocation,
  rotation and theft detection possible at all. It is 32 random bytes with no
  structure — there is nothing to read out of it, so there is nothing to trust
  from it.

## Why PyJWT and not more stdlib `hmac`

`auth/anon.py` signs one uuid with `hmac` and argues that a JWT would be
overkill. This is the other case: there are claims, there is an expiry, and the
attack that matters is the one where a caller edits `alg` to `none` or to a
public-key algorithm and re-signs. `jwt.decode` with an explicit `algorithms=`
list refuses that; a hand-rolled verifier is exactly where people forget to.

## Claims are the minimum

`sub` and the timestamps, nothing else. A JWT is base64, not encrypted, so
anything put in one is readable by whoever holds it — and there is no reason for a
phone number to be in a value that sits in a browser's memory and gets pasted into
bug reports. The row is one primary-key lookup away for anything else.
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from loguru import logger

from config import settings

# What `sub` means. Present so a future token type (a one-time voice-session
# token, say) cannot be replayed as an access token just by being signed with the
# same secret.
_ACCESS = "access"


def mint_access(user_id: int) -> tuple[str, int]:
    """An access token for a verified user. Returns `(token, ttl_seconds)`.

    The TTL comes back with the token because the client needs it to schedule a
    refresh, and reading it out of the JWT would mean the client parsing a value
    it is supposed to treat as opaque.
    """
    ttl = settings.access_token_ttl_minutes * 60
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            # A string, not an int: `sub` is a string in RFC 7519 and some
            # libraries reject a numeric one outright.
            "sub": str(user_id),
            "typ": _ACCESS,
            "iat": now,
            "exp": now + timedelta(seconds=ttl),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return token, ttl


def read_access(token: str | None) -> int | None:
    """The `user_id` inside a valid access token, or None.

    None for every failure — absent, malformed, expired, wrong signature, wrong
    type — because the caller's response to all of them is identical: fall through
    to the anonymous identity. An expired token is not an error condition in this
    product; it is a caller who has been talking for sixteen minutes.
    """
    if not token:
        return None

    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            # Explicit, and a list of exactly one. Passing the algorithm the
            # *token* names is the classic confusion bug: `alg: none` then
            # verifies, and so does an RS256 token signed with the public key.
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # Debug, not warning: expired tokens are routine and a warning per
        # expiry would bury the signature failures that actually matter.
        logger.debug(f"[auth] rejected an access token: {type(exc).__name__}")
        return None

    if claims.get("typ") != _ACCESS:
        logger.debug("[auth] rejected a token of the wrong type")
        return None

    try:
        return int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        return None


def bearer_token(header: str | None) -> str | None:
    """The token out of an `Authorization: Bearer <token>` header.

    Case-insensitive on the scheme, because clients disagree about `Bearer` vs
    `bearer` and rejecting one of them is a bug that only shows up in whichever
    client nobody tested.
    """
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


# ── Refresh tokens ──────────────────────────────────────
def mint_refresh() -> tuple[str, str]:
    """A new refresh token. Returns `(plaintext, sha256_hex)`.

    The two halves go to different places and the plaintext is never stored:
    it goes into the caller's cookie and the hash goes into the row. Split into
    a tuple rather than hashing at the call site so there is no version of this
    flow where someone writes the wrong one to Postgres.

    `token_urlsafe(32)` is 256 bits from the OS CSPRNG. Unlike the anonymous id
    this genuinely is a secret, so it is not a uuid — uuid4 is random but is
    documented as an identifier, and the distinction is worth keeping visible.
    """
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_refresh(plaintext)


def hash_refresh(plaintext: str) -> str:
    """The lookup key for a refresh token.

    A bare SHA-256, not a password hash, and that is correct here: the input is
    256 bits of uniform randomness, so there is no dictionary to run and no
    work factor worth paying. Argon2 on a random 32-byte token buys nothing and
    would add per-request cost to the refresh path.
    """
    return hashlib.sha256(plaintext.encode()).hexdigest()


def new_family() -> str:
    """An id shared by every token in one login chain. See `models/auth.py`."""
    return str(uuid.uuid4())


def refresh_cookie_kwargs() -> dict:
    """Arguments for `Response.set_cookie`, in one place.

    Stricter than the anonymous cookie in one way that matters: `samesite="strict"`
    rather than `"lax"`. The anonymous cookie has to survive a top-level navigation
    into the app because it *is* the caller's identity and losing it resets their
    quota; this one only ever has to be present on a `fetch` the app makes to its
    own refresh endpoint, so there is no reason to let it ride along on anything a
    third-party page can trigger.

    `path` is scoped to the refresh endpoint for the same reason: a credential that
    is only spent in one place should not be attached to every request, including
    the ones that carry audio.

    `secure` is off for the same localhost reason as `auth/anon.py`, and is the same
    deployment checklist item.
    """
    return {
        "key": settings.refresh_cookie_name,
        "httponly": True,
        "samesite": "strict",
        "max_age": settings.refresh_token_ttl_days * 24 * 60 * 60,
        "path": "/api/auth",
    }
