"""
Bhasha-Agent backend — FastAPI + Pipecat.

    GET    /health              corpus stats, data-layer status, retention policy
    POST   /chat                text tool-calling; the fastest way to exercise the tools
    GET    /api/quota           turns left, and the request that establishes an identity
    GET    /api/me              who the caller is, and what they have consented to
    DELETE /api/me              erasure (DPDP Act 2023)
    POST   /api/me/consent      agree to the current privacy notice
    GET    /api/me/profile      the caller's stored details
    PUT    /api/me/profile      save some of them (patch, not replace)
    DELETE /api/me/profile      forget them, keeping the account
    DELETE /api/me/history      forget the transcripts and scheme history
    GET    /api/me/eligible     which schemes the stored profile qualifies for
    POST   /api/offer           WebRTC signalling — starts one voice session per caller
    PATCH  /api/offer           trickle ICE for the above
    GET    /client              Pipecat's prebuilt dev UI, for testing without the frontend

    POST /api/auth/request-otp | verify-otp | refresh | logout   — see auth/routes.py

Every endpoint that costs a turn resolves a caller first, and resolving one never
fails: anonymous is the default state, not a rejected login. See `auth/` for who
they are and `services/quota.py` for what they are allowed.

**The conversational surface never refuses a caller; the account surface may.**
`/chat`, `/api/quota` and `/api/offer` always answer 200 and explain themselves in
the body — that is the anonymous-first promise, and the moment one of them 401s a
first-time caller meets an error instead of the product. `/api/auth/*` and
`/api/me*` are a different surface: being wrong about a one-time code, or asking
for personal data that belongs to an account you have not proved you hold, is not
the same as asking for a service. They return 400/401/403/429 through the single
`AuthError` handler below.

That is a **correction** to the narrower rule this docstring used to state ("the
four `/api/auth` endpoints are the only ones that refuse a caller"). It was
already untrue when `DELETE /api/me` started requiring a token in slice 2, and
slice 3 — 401 on reading a profile, 403 `consent_required` on writing one — makes
it untenable. The distinction that survives is the one above, and it is the one
worth defending.

Gone from v1, deliberately: `/rtc/webrtc/offer` (FastRTC), `/gradio` (a second
Stream with its own divergent VAD tuning), and `/ws/cards` with its process-wide
`_active_ws` broadcast list. UI events now go over RTVI on the caller's own
transport, so a card cannot land on a stranger's screen.

Resources — the Postgres pool, the Qdrant client, the embedder — are created once
here and handed to each session. Nothing reads them from a module global.
"""
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from auth import anon, crypto, tokens
from auth import routes as auth_routes
from auth.identity import current_user, token_verified
from auth.phone import mask
from config import settings
from models.identity import User
from services import auth as auth_service
from services import conversations as conv_service
from services import eligibility as eligibility_service
from services import profiles as profile_service
from services import quota
from services import resources as resource_factory
from services import retention
from services import schemes as scheme_service
from tools.card import CARD_EVENT
from tools.events import EventCollector
from tools.registry import ToolContext
from voice import llm
from voice.pipeline import run_session
from voice.transport import ice_servers


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the pool here rather than on the first tool call: that call would
    # otherwise pay for a Postgres connect and the embedder's first request while
    # a caller is waiting mid-sentence.
    app.state.resources = await resource_factory.create()

    try:
        stats = await scheme_service.stats(app.state.resources)
        logger.info(
            f"corpus: {stats['total_schemes']} schemes in Postgres, "
            f"{stats['indexed_schemes']} indexed in Qdrant, "
            f"{len(stats['states'])} states, {len(stats['categories'])} categories")
        if not stats["semantic_search"]:
            logger.warning("no embeddings available — search is filter-only")
    except Exception as exc:                                  # noqa: BLE001
        # Don't refuse to boot; /health is the endpoint that should report this.
        logger.warning(f"data layer unavailable: {exc}")

    logger.info(f"llm={settings.llm_model} transport={settings.transport}")

    # Said once at boot, because from the outside an unset key looks like a
    # working app that simply never remembers anything.
    if crypto.available():
        logger.info(f"[privacy] profile encryption on (key "
                    f"{crypto.fingerprint()}), retention {retention.policy()}")
    else:
        logger.warning("[privacy] PROFILE_ENCRYPTION_KEY unset — no profiles and "
                       "no transcripts will be stored")

    # The retention sweep runs from the app, not from a cron job somebody has to
    # remember to install: a documented policy that only some deployments enforce
    # is not a policy. It is a task rather than an `await` so boot is not delayed
    # by a DELETE over a large table.
    sweeper = asyncio.create_task(retention.run_forever(app.state.resources),
                                  name="retention-sweep")

    yield

    sweeper.cancel()
    try:
        await sweeper
    except asyncio.CancelledError:
        pass

    await llm.close_client()
    await app.state.resources.close()
    logger.info("shut down")


app = FastAPI(
    title="Bhasha-Agent API",
    description="Multilingual voice AI for government welfare schemes",
    version="2.0.0",
    lifespan=lifespan,
)

# Origins come from config (CORS_ORIGINS) — a wildcard is invalid alongside
# allow_credentials=True, and credentialed requests are needed from P3 onward.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router)


@app.exception_handler(auth_service.AuthError)
async def auth_error_handler(_request: Request, exc: auth_service.AuthError):
    """Turn a login failure into its HTTP form, in one place.

    A handler rather than a `try` in each of the four endpoints: the mapping from
    "what went wrong" to "which status" belongs to the error, and repeating it per
    endpoint is how one of them ends up returning 500 for a wrong code.

    `Retry-After` is a real header and clients and proxies honour it, so a rate
    limit that carries one is a rate limit a well-behaved client will respect
    without the app having to implement a timer.
    """
    headers = ({"Retry-After": str(exc.retry_after)}
               if exc.retry_after is not None else None)
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.code, "detail": exc.detail},
        headers=headers,
    )


# ── Health ──────────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        stats = await scheme_service.stats(app.state.resources)
    except Exception as exc:                                  # noqa: BLE001
        return {"status": "degraded", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok",
        "schemes": stats["total_schemes"],
        "indexed": stats["indexed_schemes"],
        "semantic_search": stats["semantic_search"],
        "states": stats["states"],
        "categories": stats["categories"],
        # Published rather than only documented: a privacy notice that states a
        # number nothing enforces is worse than one that states nothing, so the
        # notice and this endpoint read the same source (services/retention.py).
        "privacy": {
            "profiles_enabled": crypto.available(),
            "consent_version": settings.consent_version,
            "retention": retention.policy(),
        },
    }


# ── Text chat ───────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    language: str = "en"
    # Send the same id on every turn of a thread and the thread is persisted the
    # way a voice call is. Omit it and nothing is stored, which keeps every
    # existing caller of this endpoint behaving exactly as it did.
    #
    # It is also what makes slice 3 verifiable without a live WebRTC call: the
    # persistence path can be exercised over cheap HTTP instead of synthesising
    # speech through aiortc. That it doubles as the foundation of P4's text/browse
    # mode is the reason it is a real feature rather than a test hook.
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    history: list[dict]
    timings: dict = {}
    card: dict | None = None
    # Present on every reply so a client can show the allowance without polling
    # a second endpoint. `quota.allowed` false means this reply is the refusal.
    quota: dict = {}
    # Echoed back when the thread is being persisted, absent when it is not — so a
    # client can tell "this conversation is remembered" from "this one is not"
    # without knowing the rules about phone numbers and encryption keys.
    conversation_id: str | None = None


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest, user: User = Depends(current_user)):
    """One text turn with tool-calling. No audio, no session.

    The card comes back in this response instead of being pushed anywhere. v1
    broadcast it to every open WebSocket under the name `show_card`, which the
    frontend does not handle — so REST-triggered cards were silently dropped.

    A text turn spends from the same allowance a spoken one does. Anything else
    would make `/chat` the way around the quota, and it is a public endpoint.

    Pass `conversation_id` and the thread is persisted and the caller's stored
    profile is injected, exactly as on the voice path. Both are silently skipped
    for a caller who has no account to hang them off.
    """
    verdict = await quota.consume(app.state.resources, user)

    # 200, not 429. The caller is a person mid-conversation, not a misbehaving
    # client, and the frontend renders this in the chat thread like any other
    # reply. A 429 would surface as an error toast, which is the wrong reading of
    # "you have used your free turns".
    if not verdict.allowed:
        return ChatResponse(
            response=_quota_message(verdict),
            history=req.history,
            quota=_quota_payload(verdict),
        )

    persisting = False
    if req.conversation_id:
        persisting = await conv_service.start(
            app.state.resources, user, channel="text",
            conversation_id=req.conversation_id)

    notice = await profile_service.session_context(app.state.resources, user)

    collector = EventCollector()
    ctx = ToolContext(resources=app.state.resources, deliver=collector,
                      user=user,
                      conversation_id=req.conversation_id if persisting else None)

    t0 = time.perf_counter()
    response_text, history = await llm.chat(
        ctx,
        user_message=req.message,
        conversation_history=req.history.copy(),
        detected_language=req.language,
        profile_notice=notice,
    )
    llm_ms = round((time.perf_counter() - t0) * 1000)

    if persisting:
        # After the reply is in hand, and not allowed to fail the turn: an answer
        # the caller already has cannot be un-given because a row would not write.
        try:
            await conv_service.append(
                app.state.resources, req.conversation_id,
                conv_service.turn_pairs(req.message, response_text))
            # A text thread has no hang-up, so every turn stamps the end. The row
            # therefore always reflects the last turn that happened, which is the
            # closest thing to an end this channel has.
            await conv_service.finish(
                app.state.resources, req.conversation_id, reason="text_turn",
                language=req.language)
        except Exception as exc:                              # noqa: BLE001
            logger.warning(f"could not persist the text turn: "
                           f"{type(exc).__name__}: {exc}")

    return ChatResponse(
        response=response_text,
        history=history,
        timings={"llm_ms": llm_ms},
        card=collector.first(CARD_EVENT),
        quota=_quota_payload(verdict),
        conversation_id=req.conversation_id if persisting else None,
    )


# ── Quota ───────────────────────────────────────────────
@app.get("/api/quota")
async def quota_status(user: User = Depends(current_user)):
    """How much allowance is left, without spending any.

    Also the endpoint that establishes an anonymous identity: calling it sets the
    cookie, so a client can do it once at load and have the id in place before the
    caller says anything.
    """
    return _quota_payload(await quota.peek(app.state.resources, user))


# ── The caller ──────────────────────────────────────────
@app.get("/api/me")
async def me(user: User = Depends(current_user),
             verified: bool = Depends(token_verified)):
    """Who the caller is, as far as they are allowed to be told.

    `authenticated` reports whether this *request* proved it holds an access
    token, not whether the identity happens to have a phone number on it. Those
    differ for exactly one caller: someone who logged in on this device and is now
    sending only the anonymous cookie. They get the larger allowance either way
    (see `auth/identity.py`), but the phone number is personal data and the cookie
    is not proof of anything about a person.

    Slice 3 adds the profile here, which is why that distinction is worth having
    before there is anything sensitive behind it.
    """
    allowance = await quota.peek(app.state.resources, user)

    # The profile itself is not here — `GET /api/me/profile` is — but whether
    # there is one has to be, or a client cannot decide between "fill this in"
    # and "review this" without asking for the sensitive payload first.
    profile = await profile_service.load(app.state.resources, user) if verified else None
    eligible_count = (await eligibility_service.cached_count(
        app.state.resources, user, profile) if profile else None)

    return {
        "authenticated": verified,
        "phone": mask(user.phone_e164) if verified else None,
        "consent": {
            "version": user.consent_version,
            "at": user.consent_at.isoformat() if user.consent_at else None,
            # Not `version is not None`: publishing a new notice has to make every
            # existing agreement stale, and a client that only checked for presence
            # would never show the new one.
            "current": profile_service.consent_current(user),
            "required_version": settings.consent_version,
        },
        "profile": {
            "supported": crypto.available(),
            "has_profile": profile is not None,
            "fields": list(profile.known) if profile else [],
            "updated_at": (profile.updated_at.isoformat()
                           if profile and profile.updated_at else None),
            # None means "not worked out yet", which is a different thing to show
            # someone than zero. See services/eligibility.py::cached_count.
            "eligible_count": eligible_count,
        },
        "quota": _quota_payload(allowance),
    }


# ── Consent, profile, memory ────────────────────────────
# Everything below discloses or stores sensitive personal data, so all of it
# requires a *verified access token* rather than merely an identity that happens
# to carry a phone number (see `GET /api/me`). This is the account surface, and
# the account surface is allowed to refuse — the conversational surface
# (`/chat`, `/api/quota`, `/api/offer`) still never does.

def _require_verified(verified: bool) -> None:
    if not verified:
        raise auth_service.AuthError(
            401, "not_authenticated",
            "Sign in with your phone number to see or change your details.")


@app.post("/api/me/consent")
async def accept_consent(user: User = Depends(current_user),
                         verified: bool = Depends(token_verified)):
    """Record agreement to the current privacy notice.

    A separate call rather than a flag on the first profile write, because DPDP
    consent has to be a deliberate act with a timestamp against a version — not
    something inferred from someone filling in a form. `services/profiles.py`
    enforces it on every write path, including P5's background extractor.
    """
    _require_verified(verified)
    at = await profile_service.record_consent(app.state.resources, user)
    return {"consent": {"version": settings.consent_version,
                        "at": at.isoformat(), "current": True}}


@app.get("/api/me/profile")
async def get_profile(user: User = Depends(current_user),
                      verified: bool = Depends(token_verified)):
    """The caller's stored details, decrypted, in full.

    The DPDP right of access. Full values, not the masked summary `GET /api/me`
    carries: this is the caller asking to see their own data, and showing them a
    redacted version of it would defeat the point of the right.
    """
    _require_verified(verified)
    profile = await profile_service.load(app.state.resources, user)
    return {
        "supported": crypto.available(),
        "has_profile": profile is not None,
        "facts": profile.facts if profile else {},
        "fields": list(profile_service.FIELDS),
        "version": profile.version if profile else 0,
        "consent_version": profile.consent_version if profile else None,
        "updated_at": (profile.updated_at.isoformat()
                       if profile and profile.updated_at else None),
        "retention": "kept until you delete it; transcripts expire on their own",
    }


@app.put("/api/me/profile")
async def put_profile(patch: dict[str, Any], user: User = Depends(current_user),
                      verified: bool = Depends(token_verified)):
    """Merge details into the profile. PUT, but a patch — see `profiles.save`.

    The body is taken as a plain object rather than a Pydantic model on purpose:
    `profiles._clean` already owns validation, and it answers with this app's own
    400 shape (`unknown_fields`, `bad_value`) and a sentence a client can show a
    user. A model here would pre-empt it with a 422 nobody can render.

    Saving invalidates the cached eligibility verdicts — inside `profiles.save`,
    not here, because their reasons quote facts that any writer can change.
    """
    _require_verified(verified)
    profile = await profile_service.save(app.state.resources, user, patch)
    return {
        "saved": True,
        "facts": profile.facts,
        "version": profile.version,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


@app.delete("/api/me/profile")
async def delete_profile(user: User = Depends(current_user),
                         verified: bool = Depends(token_verified)):
    """Forget the caller's details, keeping their account and their allowance."""
    _require_verified(verified)
    removed = await profile_service.erase(app.state.resources, user)
    return {"deleted": removed}


@app.delete("/api/me/history")
async def delete_history(user: User = Depends(current_user),
                         verified: bool = Depends(token_verified)):
    """Forget what was said and what was looked at, keeping the account.

    The third of the three erasure granularities, and they are three rather than
    one because they are three different things a person might want: forget this
    conversation, forget what you know about me, close my account.
    """
    _require_verified(verified)
    removed = await conv_service.erase_all(app.state.resources, user)
    return {"deleted": removed}


@app.get("/api/me/eligible")
async def eligible_schemes(limit: int = 20, user: User = Depends(current_user),
                           verified: bool = Depends(token_verified)):
    """Every scheme the caller's stored profile qualifies them for.

    Deterministic rules over the live corpus, never vector similarity — a verdict
    a citizen may act on cannot come from a ranking. `eligible_count` of 0 with an
    empty `profile_fields` means there is nothing stored to evaluate against, not
    that they qualify for nothing.
    """
    _require_verified(verified)
    return await eligibility_service.evaluate_all(
        app.state.resources, user, limit=max(1, min(limit, 100)))


@app.delete("/api/me")
async def delete_me(response: Response, user: User = Depends(current_user),
                    verified: bool = Depends(token_verified)):
    """Erase the caller's account and everything referencing it (DPDP Act 2023).

    Requires a verified access token, and this is the one place where refusing an
    anonymous caller is right rather than a regression: an anonymous identity has
    nothing to erase — no phone number, no profile, no consent record — and
    honouring the request would delete a quota bucket, which is a way to reset an
    allowance rather than a way to exercise a data right.

    Both cookies are cleared, because leaving the anonymous one behind would leave
    the caller carrying an id whose row no longer exists.
    """
    if not verified:
        raise auth_service.AuthError(
            401, "not_authenticated",
            "Sign in first — there is no account to delete.")

    await auth_service.delete_account(app.state.resources, user)

    refresh = tokens.refresh_cookie_kwargs()
    response.delete_cookie(key=refresh["key"], path=refresh["path"],
                           httponly=True, samesite=refresh["samesite"])
    anon_cookie = anon.cookie_kwargs()
    response.delete_cookie(key=anon_cookie["key"], path=anon_cookie["path"],
                           httponly=True, samesite=anon_cookie["samesite"])
    return {"deleted": True}


def _quota_payload(verdict: quota.Verdict) -> dict:
    """The wire shape for an allowance. Deliberately not the `User` row — a client
    has no business knowing the row id, and a phone number must never leave the
    server in a quota response."""
    return {
        "allowed": verdict.allowed,
        "used": verdict.turns_used,
        "limit": verdict.limit,
        "remaining": verdict.remaining,
        "authenticated": verdict.authenticated,
        "resets_at": verdict.resets_at.isoformat() if verdict.resets_at else None,
    }


def _quota_message(verdict: quota.Verdict) -> str:
    """What to say when the allowance is gone.

    English only, and that is a known gap: the voice path speaks this in the
    caller's language via the LLM (see `voice/quota_gate.py`), but `/chat`'s
    refusal never reaches the model, so there is nothing here to translate it.
    Wiring it to the caller's `language` is a P4 item, tracked rather than
    silently ignored.
    """
    if verdict.authenticated:
        return ("You have used all your turns for now. They will refresh shortly "
                "— thank you for your patience.")
    return ("You have used your free turns. Sign in with your phone number to "
            "continue and get many more.")


# ── Voice: WebRTC signalling ────────────────────────────
# One handler for the process; one connection, one pipeline, one context per
# caller. That is the multi-tenancy fix — v1 built its handler once at import.
_webrtc = SmallWebRTCRequestHandler(ice_servers=ice_servers() or None)


@app.post("/api/offer")
async def offer(
    request: SmallWebRTCRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_user),
):
    """Answer a caller's SDP offer and start their session in the background.

    The identity is resolved *here*, not inside the pipeline, because this is the
    only part of a call that is an HTTP request — it is what carries the cookie,
    and it is where a new one can still be set. The pipeline is handed the result
    and never has to think about credentials again.

    The turn allowance is not checked here either. A caller with nothing left
    still gets a connection, hears why in words, and the call closes itself; see
    `voice/quota_gate.py`. Refusing the offer would give them a UI that fails to
    connect, which reads as a broken app rather than an exhausted allowance.
    """
    conversation_id = str(uuid.uuid4())

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        # A BackgroundTask, so this request returns the SDP answer immediately;
        # the session then lives as long as the call.
        background_tasks.add_task(
            run_session,
            app.state.resources,
            user=user,
            webrtc_connection=connection,
            conversation_id=conversation_id,
        )

    return await _webrtc.handle_web_request(
        request=request, webrtc_connection_callback=on_connection)


@app.patch("/api/offer")
async def ice_candidate(request: SmallWebRTCPatchRequest):
    """Trickle ICE. v1's frontend waited a hardcoded 1500ms instead."""
    await _webrtc.handle_patch_request(request)
    return {"status": "success"}


# ── Dev client ──────────────────────────────────────────
# Pipecat's prebuilt RTVI UI. This is the P2 verification surface: open it in two
# browsers and check that each call is its own conversation. The Next.js
# frontend moves onto the Pipecat React SDK in P4.
try:
    from pipecat_ai_prebuilt.frontend import PipecatPrebuiltUI

    app.mount("/client", PipecatPrebuiltUI)
    logger.info("dev client mounted at /client")
except ImportError:
    logger.info("pipecat-ai-prebuilt not installed — /client unavailable")


if __name__ == "__main__":
    import uvicorn

    # reload=False on purpose: a reload drops every live call, and the reloader's
    # double import would build two of everything in the lifespan.
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
