"""
The login endpoints. Mounted at `/api/auth` by `main.py`.

    POST /api/auth/request-otp   send a code to a phone number
    POST /api/auth/verify-otp    exchange a code for a session
    POST /api/auth/refresh       exchange the refresh cookie for a new access token
    POST /api/auth/logout        revoke the session and clear the cookie

They live here rather than in `main.py` because the four of them are one flow and
they are the only endpoints in the app that refuse a caller — keeping them together
keeps that exception in one file. Everything they decide is in `services/auth.py`;
this module is the HTTP shape and nothing else.

## Why the access token goes in the body and the refresh token in a cookie

They have opposite requirements. The access token has to be attached to requests
that JavaScript makes, including the WebRTC offer, so the client has to be able to
read it — a cookie it cannot read is no use, and a cookie it *can* read is an XSS
away from being stolen with no compensating benefit. It is short-lived precisely so
that holding it in memory is acceptable.

The refresh token is the opposite: it is long-lived, it is only ever sent to one
endpoint, and JavaScript has no reason to touch it. So it is httpOnly, `strict`,
and scoped to `/api/auth` — an XSS can use the app, but it cannot walk away with a
thirty-day credential.
"""
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from auth import tokens
from auth.identity import current_user
from auth.phone import mask
from config import settings
from models.identity import User
from services import auth as auth_service
from services import quota

router = APIRouter(prefix="/api/auth", tags=["auth"])


class PhoneRequest(BaseModel):
    # Not validated by pattern here: `auth/phone.py` accepts several spellings of
    # the same number and a regex in the schema would reject inputs the normaliser
    # handles fine, with a 422 the client cannot explain to a user.
    phone: str = Field(min_length=6, max_length=20)


class OtpSentResponse(BaseModel):
    sent: bool = True
    # Masked, so the client can show "code sent to +*****3210" and the caller can
    # catch a typo before waiting for an SMS that will never come.
    phone: str
    expires_in: int
    retry_after: int
    # Present only when `otp_dev_echo` is on AND the provider is the stub. It is in
    # the schema rather than smuggled in, so that anyone reading the API can see
    # this exists and check that it is off.
    dev_code: str | None = None


class VerifyRequest(PhoneRequest):
    code: str = Field(min_length=4, max_length=8)


class SessionResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    phone: str | None = None
    # The allowance the caller just unlocked, so the client does not have to make a
    # second request to find out that signing in was worth it.
    quota: dict = {}


@router.post("/request-otp", response_model=OtpSentResponse)
async def request_otp(body: PhoneRequest, request: Request):
    """Send a one-time code.

    Deliberately does **not** say whether the number already has an account. That
    would turn this endpoint into a way to test whether a given phone number is a
    user of a welfare-scheme app, which is exactly the kind of inference this
    audience cannot afford to have leaked.
    """
    phone, dev_code = await auth_service.request_otp(
        request.app.state.resources, body.phone)
    return OtpSentResponse(
        phone=mask(phone),
        expires_in=settings.otp_ttl_seconds,
        retry_after=settings.otp_resend_cooldown_seconds,
        dev_code=dev_code,
    )


@router.post("/verify-otp", response_model=SessionResponse)
async def verify_otp(body: VerifyRequest, request: Request, response: Response,
                     user: User = Depends(current_user)):
    """Exchange a code for a session.

    `current_user` runs first, which is what makes promotion work: the identity
    that has been talking to the agent anonymously is the row that acquires the
    phone number, so the turns it has already spent this window carry over instead
    of being forgotten. See `services/auth.py`.
    """
    session = await auth_service.verify_otp(
        request.app.state.resources, body.phone, body.code, user)

    response.set_cookie(value=session.refresh_token,
                        **tokens.refresh_cookie_kwargs())
    allowance = await quota.peek(request.app.state.resources, session.user)
    return SessionResponse(
        access_token=session.access_token,
        expires_in=session.expires_in,
        phone=mask(session.user.phone_e164),
        quota={
            "used": allowance.turns_used,
            "limit": allowance.limit,
            "remaining": allowance.remaining,
            "authenticated": True,
        },
    )


@router.post("/refresh", response_model=SessionResponse)
async def refresh(request: Request, response: Response):
    """A new access token, and a rotated refresh cookie.

    No `current_user` dependency: this endpoint authenticates on the refresh cookie
    alone. Resolving an identity here would mint an anonymous one for a caller whose
    session has expired, which is a row created as a side effect of a 401.
    """
    session = await auth_service.rotate(
        request.app.state.resources,
        request.cookies.get(settings.refresh_cookie_name))

    response.set_cookie(value=session.refresh_token,
                        **tokens.refresh_cookie_kwargs())
    allowance = await quota.peek(request.app.state.resources, session.user)
    return SessionResponse(
        access_token=session.access_token,
        expires_in=session.expires_in,
        phone=mask(session.user.phone_e164),
        quota={
            "used": allowance.turns_used,
            "limit": allowance.limit,
            "remaining": allowance.remaining,
            "authenticated": True,
        },
    )


@router.post("/logout")
async def logout(request: Request, response: Response):
    """End the session. Always 200 — asking to be signed out cannot fail.

    The cookie is deleted with the same `path` and `samesite` it was set with.
    A `delete_cookie` that disagrees on path silently does nothing, leaving the
    caller with a revoked-but-present credential and a confusing 401 on their next
    refresh.
    """
    await auth_service.logout(
        request.app.state.resources,
        request.cookies.get(settings.refresh_cookie_name))

    kwargs = tokens.refresh_cookie_kwargs()
    response.delete_cookie(key=kwargs["key"], path=kwargs["path"],
                           httponly=True, samesite=kwargs["samesite"])
    return {"signed_out": True}
