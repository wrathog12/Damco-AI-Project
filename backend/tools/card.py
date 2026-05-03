"""
show_scheme_card tool — triggers frontend card popup via WebSocket.
"""
import json
from knowledge import loader

# This will be set by main.py when a WebSocket connection is active
_ws_send_callback = None


def set_ws_callback(callback):
    """Register the WebSocket send function (called from main.py)."""
    global _ws_send_callback
    _ws_send_callback = callback


async def show_scheme_card(scheme_id: str, language: str = "en") -> str:
    """
    1. Look up scheme data
    2. Push card payload to frontend via WebSocket
    3. Return confirmation for LLM to speak
    """
    scheme = loader.get_by_id(scheme_id)

    if not scheme:
        return json.dumps({"error": f"Scheme '{scheme_id}' not found."})

    card_payload = {
        "type": "show_card",
        "scheme": scheme,
        "language": language,
    }

    # Push to frontend if WebSocket is connected
    if _ws_send_callback:
        try:
            await _ws_send_callback(json.dumps(card_payload, ensure_ascii=False))
        except Exception as e:
            print(f"[CARD] WebSocket send failed: {e}")

    return json.dumps({
        "status": "card_shown",
        "scheme_name": scheme.get("scheme_name"),
        "message": "Scheme card has been displayed on the user's screen.",
    })


def show_scheme_card_sync(scheme_id: str, language: str = "en") -> str:
    """Synchronous version for tool dispatcher (queues the WS message)."""
    scheme = loader.get_by_id(scheme_id)

    if not scheme:
        return json.dumps({"error": f"Scheme '{scheme_id}' not found."})

    # Store pending card for the pipeline to send
    global _pending_card
    _pending_card = {
        "type": "show_card",
        "scheme": scheme,
        "language": language,
    }

    return json.dumps({
        "status": "card_shown",
        "scheme_name": scheme.get("scheme_name"),
        "message": "Scheme card has been displayed on the user's screen.",
    })


# Pending card data (read and cleared by pipeline)
_pending_card = None


def pop_pending_card() -> dict | None:
    """Return and clear any pending card payload."""
    global _pending_card
    card = _pending_card
    _pending_card = None
    return card
