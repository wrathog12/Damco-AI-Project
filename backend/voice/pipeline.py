"""
Voice pipeline — FastRTC ReplyOnPause generator.

Flow: STT → streaming LLM (sentence-by-sentence) → TTS per sentence.

TTS of sentence 1 plays while LLM generates sentence 2.
This overlap dramatically reduces perceived latency.

Barge-in is handled by FastRTC: if the user starts speaking while
the generator is yielding audio, FastRTC interrupts the generator.
"""
import json
import time
import numpy as np
from voice import stt, llm, tts
from tools.card import pop_pending_card
from tools.end_call import pop_pending_end


# ── Shared WebSocket list (set by main.py) ─────────────────
_active_ws_list: list = []
_main_loop = None  # reference to uvicorn's event loop


def set_ws_list(ws_list: list):
    """Called from main.py to share the active WebSocket connections."""
    global _active_ws_list
    _active_ws_list = ws_list


def set_event_loop(loop):
    """Called from main.py to share the main asyncio event loop."""
    global _main_loop
    _main_loop = loop


def _push_ws_event(data: dict):
    """Push a JSON event to all connected frontend WebSockets (thread-safe)."""
    import asyncio

    payload = json.dumps(data, ensure_ascii=False)

    if not _active_ws_list:
        print("[WS] No active WebSocket connections, skipping push")
        return

    async def _send_all():
        for ws in _active_ws_list:
            try:
                await ws.send_text(payload)
            except Exception as e:
                print(f"[WS] Send failed: {e}")

    if _main_loop and _main_loop.is_running():
        # Schedule on the main event loop from this sync thread
        future = asyncio.run_coroutine_threadsafe(_send_all(), _main_loop)
        try:
            future.result(timeout=3.0)  # wait up to 3s for delivery
        except Exception as e:
            print(f"[WS] Push failed: {e}")
    else:
        print("[WS] Main event loop not available, cannot push event")

# ── Activity tracking (for inactivity timer) ──────────────
last_activity_ts: float = 0.0   # epoch seconds of last voice trigger


# ── Per-session state ──────────────────────────────────────

def make_voice_handler():
    """
    Factory: returns a generator function with its own
    conversation_history (one per WebRTC session).
    """
    conversation_history: list[dict] = []

    def voice_response(audio: tuple[int, np.ndarray]):
        """
        Called by ReplyOnPause when user stops speaking.

        Pipeline: STT → LLM (streaming sentences) → TTS per sentence.

        Each sentence is TTS'd and yielded immediately, so the user
        hears the first sentence while the LLM is still generating
        the rest.
        """
        nonlocal conversation_history
        global last_activity_ts
        last_activity_ts = time.time()   # mark activity

        sr, audio_array = audio

        # Flatten if 2D (FastRTC sends (1, N))
        if audio_array.ndim == 2:
            audio_array = audio_array.flatten()

        # ── Notify frontend: processing started ──────────
        _push_ws_event({"type": "status_change", "status": "processing"})

        # ── Stage 1: STT ─────────────────────────────────
        t0 = time.perf_counter()
        transcript, detected_lang = stt.transcribe(audio_array, sr)
        stt_ms = round((time.perf_counter() - t0) * 1000)

        if not transcript.strip():
            print("[PIPELINE] Empty transcript, skipping")
            _push_ws_event({"type": "status_change", "status": "listening"})
            return

        print(f"[STT] ({detected_lang}) \"{transcript}\"  [{stt_ms}ms]")

        # ── Stage 2+3: Streaming LLM → Sentence TTS ─────
        t_llm_start = time.perf_counter()
        sentence_count = 0
        total_tts_ms = 0
        total_samples = 0
        first_audio_ms = None

        card_pushed = False
        end_pushed = False

        try:
            for sentence, is_final in llm.chat_streaming(
                user_message=transcript,
                conversation_history=conversation_history,
                detected_language=detected_lang,
            ):
                sentence_count += 1
                llm_ms = round((time.perf_counter() - t_llm_start) * 1000)

                if sentence_count == 1:
                    print(f"[LLM] First sentence in {llm_ms}ms: \"{sentence}\"")

                # ── Check for pending card and PUSH to frontend ──
                # Tool calls complete before streaming, so card is ready immediately.
                # Check on every sentence (pop returns None after first call).
                if not card_pushed:
                    pending_card = pop_pending_card()
                    if pending_card:
                        pending_card["type"] = "show_scheme_card"
                        scheme_name = pending_card.get("scheme", {}).get("scheme_name", "unknown")
                        print(f"[CARD] Pushing card to frontend: {scheme_name}")
                        print(f"[CARD] Active WS connections: {len(_active_ws_list)}")
                        _push_ws_event(pending_card)
                        card_pushed = True

                # Check for end_call signal
                if not end_pushed and is_final:
                    pending_end = pop_pending_end()
                    if pending_end:
                        print(f"[END_CALL] Ending call: {pending_end.get('reason')}")
                        _push_ws_event(pending_end)
                        end_pushed = True

                # ── TTS this sentence immediately ─────────────
                if not sentence.strip():
                    continue

                t_tts = time.perf_counter()
                chunk_count = 0

                for audio_chunk in tts.synthesize_streaming(sentence):
                    chunk_count += 1
                    total_samples += len(audio_chunk)

                    if first_audio_ms is None:
                        first_audio_ms = round((time.perf_counter() - t_llm_start) * 1000)
                        _push_ws_event({"type": "status_change", "status": "speaking"})

                    # FastRTC expects (sample_rate, ndarray with shape (1, N))
                    yield (24000, audio_chunk.reshape(1, -1))

                tts_ms = round((time.perf_counter() - t_tts) * 1000)
                total_tts_ms += tts_ms

        except Exception as e:
            print(f"[PIPELINE] LLM/TTS error (recovering): {e}")
            # Speak a fallback message so the user isn't left hanging
            fallback = "Sorry, I had a small issue. Could you please repeat that?"
            try:
                for audio_chunk in tts.synthesize_streaming(fallback):
                    yield (24000, audio_chunk.reshape(1, -1))
            except Exception:
                pass  # If TTS also fails, just stay silent

        # ── Notify frontend: back to listening ─────────
        _push_ws_event({"type": "status_change", "status": "listening"})

        # ── Summary ──────────────────────────────────────
        total_ms = round((time.perf_counter() - t_llm_start) * 1000) + stt_ms
        print(
            f"[PIPELINE] Total: {total_ms}ms  "
            f"(STT={stt_ms} TTFA={first_audio_ms or 0} "
            f"sentences={sentence_count} TTS_total={total_tts_ms})"
        )

    return voice_response


def make_startup_greeting():
    """
    Factory: returns a generator function that speaks
    a welcome message when the WebRTC connection starts.
    """
    def startup():
        greeting = (
            "Hello! I am Bhasha Agent, your voice assistant for "
            "government welfare schemes. "
            "Which state are you from, and what kind of scheme "
            "are you looking for?"
        )
        print("[STARTUP] Speaking greeting...")
        for audio_chunk in tts.synthesize_streaming(greeting):
            yield (24000, audio_chunk.reshape(1, -1))
        print("[STARTUP] Greeting complete")

    return startup
