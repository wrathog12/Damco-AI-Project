"""
Bhasha-Agent backend — FastAPI + Pipecat.

    GET   /health      corpus stats and data-layer status
    POST  /chat        text tool-calling; the fastest way to exercise the tools
    POST  /api/offer   WebRTC signalling — starts one voice session per caller
    PATCH /api/offer   trickle ICE for the above
    GET   /client      Pipecat's prebuilt dev UI, for testing without the frontend

Gone from v1, deliberately: `/rtc/webrtc/offer` (FastRTC), `/gradio` (a second
Stream with its own divergent VAD tuning), and `/ws/cards` with its process-wide
`_active_ws` broadcast list. UI events now go over RTVI on the caller's own
transport, so a card cannot land on a stranger's screen.

Resources — the Postgres pool, the Qdrant client, the embedder — are created once
here and handed to each session. Nothing reads them from a module global.
"""
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from config import settings
from services import resources as resource_factory
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

    yield

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
    }


# ── Text chat ───────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    language: str = "en"


class ChatResponse(BaseModel):
    response: str
    history: list[dict]
    timings: dict = {}
    card: dict | None = None


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest):
    """One text turn with tool-calling. No audio, no session.

    The card comes back in this response instead of being pushed anywhere. v1
    broadcast it to every open WebSocket under the name `show_card`, which the
    frontend does not handle — so REST-triggered cards were silently dropped.
    """
    collector = EventCollector()
    ctx = ToolContext(resources=app.state.resources, deliver=collector)

    t0 = time.perf_counter()
    response_text, history = await llm.chat(
        ctx,
        user_message=req.message,
        conversation_history=req.history.copy(),
        detected_language=req.language,
    )
    llm_ms = round((time.perf_counter() - t0) * 1000)

    return ChatResponse(
        response=response_text,
        history=history,
        timings={"llm_ms": llm_ms},
        card=collector.first(CARD_EVENT),
    )


# ── Voice: WebRTC signalling ────────────────────────────
# One handler for the process; one connection, one pipeline, one context per
# caller. That is the multi-tenancy fix — v1 built its handler once at import.
_webrtc = SmallWebRTCRequestHandler(ice_servers=ice_servers() or None)


@app.post("/api/offer")
async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    """Answer a caller's SDP offer and start their session in the background."""
    conversation_id = str(uuid.uuid4())

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        # A BackgroundTask, so this request returns the SDP answer immediately;
        # the session then lives as long as the call.
        background_tasks.add_task(
            run_session,
            app.state.resources,
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
