"""
Bhasha-Agent Backend — FastAPI + FastRTC WebRTC voice pipeline.

Endpoints:
  GET  /health           -> health check + KB stats
  POST /chat             -> text-based tool-calling endpoint (testing)
  WS   /ws/cards         -> WebSocket for card push events
  POST /rtc/webrtc/offer -> FastRTC WebRTC signaling (auto-mounted)
  GET  /gradio/          -> Gradio test UI for voice (sanity check)
"""
import json
import time
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from knowledge import loader
from voice import llm
from tools.card import pop_pending_card


# ── Lifespan: load KB on startup ────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    count = loader.load(settings.kb_path)
    print(f"[SERVER] Knowledge base loaded: {count} schemes")
    print(f"[SERVER] LLM model: {settings.llm_model}")

    # Share the WebSocket list and event loop with the voice pipeline
    from voice.pipeline import set_ws_list, set_event_loop
    set_ws_list(_active_ws)
    set_event_loop(asyncio.get_event_loop())

    # Start inactivity timer
    inactivity_task = asyncio.create_task(_inactivity_watcher())

    yield

    # Shutdown
    inactivity_task.cancel()
    print("[SERVER] Shutting down")


INACTIVITY_TIMEOUT = 120  # seconds


async def _inactivity_watcher():
    """Background task: if no voice activity for 35s after a call starts, push end_call."""
    import voice.pipeline as pipeline

    while True:
        await asyncio.sleep(5)  # check every 5 seconds

        ts = pipeline.last_activity_ts
        if ts == 0.0:
            continue  # no call has started yet

        elapsed = time.time() - ts
        if elapsed >= INACTIVITY_TIMEOUT and _active_ws:
            print(f"[INACTIVITY] No activity for {int(elapsed)}s — ending call")
            payload = json.dumps({"type": "end_call", "reason": "inactivity"})
            for ws in _active_ws:
                try:
                    await ws.send_text(payload)
                except Exception:
                    pass
            # Reset so we don't spam
            pipeline.last_activity_ts = 0.0


app = FastAPI(
    title="Bhasha-Agent API",
    description="Multilingual voice AI for government welfare schemes",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Active WebSocket connections ────────────────────────
_active_ws: list[WebSocket] = []


# ── Health check ────────────────────────────────────────
@app.get("/health")
async def health():
    stats = loader.get_stats()
    return {
        "status": "ok",
        "schemes": stats["total_schemes"],
        "states": stats["states"],
        "categories": stats["categories"],
    }


# ── Text chat endpoint (for testing without voice) ─────
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
    """
    Text-based chat endpoint for testing the LLM + tools pipeline.
    Send a message, get a response with tool-calling.
    """
    import time

    t0 = time.perf_counter()
    response_text, history = llm.chat(
        user_message=req.message,
        conversation_history=req.history.copy(),
        detected_language=req.language,
    )
    llm_ms = round((time.perf_counter() - t0) * 1000)

    # Check for pending card
    card = pop_pending_card()

    # If card, broadcast to any connected WebSocket
    if card:
        for ws in _active_ws:
            try:
                await ws.send_text(json.dumps(card, ensure_ascii=False))
            except Exception:
                pass

    return ChatResponse(
        response=response_text,
        history=history,
        timings={"llm_ms": llm_ms},
        card=card,
    )


# ── WebSocket for card events ──────────────────────────
@app.websocket("/ws/cards")
async def card_websocket(websocket: WebSocket):
    await websocket.accept()
    _active_ws.append(websocket)
    print(f"[WS] Card WebSocket connected ({len(_active_ws)} active)")

    try:
        while True:
            # Listen for messages from frontend (e.g., card dismiss)
            data = await websocket.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "dismiss_card":
                print("[WS] Card dismissed by user")
                # Could signal pipeline to resume here

    except WebSocketDisconnect:
        _active_ws.remove(websocket)
        print(f"[WS] Card WebSocket disconnected ({len(_active_ws)} active)")


# ══════════════════════════════════════════════════════════
# FastRTC WebRTC Voice Pipeline
# ══════════════════════════════════════════════════════════
from fastrtc import Stream, ReplyOnPause, AlgoOptions, SileroVadOptions
from voice.pipeline import make_voice_handler, make_startup_greeting

# Create handler with session state
handler_fn = make_voice_handler()
startup_fn = make_startup_greeting()

# FastRTC Stream with ReplyOnPause + barge-in
stream = Stream(
    handler=ReplyOnPause(
        handler_fn,
        startup_fn=startup_fn,
        can_interrupt=True,
        algo_options=AlgoOptions(
            audio_chunk_duration=0.6,
            started_talking_threshold=0.3,
            speech_threshold=0.15,
        ),
        model_options=SileroVadOptions(
            threshold=0.55,
            min_speech_duration_ms=300,
            min_silence_duration_ms=150,
        ),
    ),
    modality="audio",
    mode="send-receive",
)

# Mount WebRTC signaling at /rtc (adds /rtc/webrtc/offer)
stream.mount(app, path="/rtc")
print("[SERVER] FastRTC WebRTC mounted at /rtc/webrtc/offer")

# ── Gradio test UI (sanity check) ──────────────────────
# Access at http://localhost:8000/gradio/ for quick voice testing
# without needing the Next.js frontend
try:
    import gradio as gr

    gradio_stream = Stream(
        handler=ReplyOnPause(
            make_voice_handler(),
            startup_fn=make_startup_greeting(),
            can_interrupt=True,
            algo_options=AlgoOptions(
                audio_chunk_duration=0.6,
                started_talking_threshold=0.2,
                speech_threshold=0.1,
            ),
            model_options=SileroVadOptions(
                threshold=0.5,
                min_speech_duration_ms=250,
                min_silence_duration_ms=100,
            ),
        ),
        modality="audio",
        mode="send-receive",
    )

    gradio_app = gr.Blocks()
    with gradio_app:
        gr.Markdown("# Bhasha-Agent Voice Test")
        gr.Markdown("Speak to test the voice pipeline. Barge-in enabled.")
        gradio_stream.ui.render()

    app = gr.mount_gradio_app(app, gradio_app, path="/gradio")
    print("[SERVER] Gradio test UI mounted at /gradio")
except Exception as e:
    print(f"[SERVER] Gradio UI not available: {e}")


# ── Run ─────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,  # Disable reload — FastRTC state doesn't survive reloads
    )
