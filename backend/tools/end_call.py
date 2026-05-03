"""
end_call tool — signals the frontend to terminate the voice session.
"""
import json

# Pending end-call signal (read and cleared by pipeline)
_pending_end: dict | None = None


def end_call_sync(reason: str = "user_requested") -> str:
    """
    Called by the LLM when the user indicates they want to end the call.
    Queues an end signal for the pipeline to push over WebSocket.
    """
    global _pending_end
    _pending_end = {
        "type": "end_call",
        "reason": reason,
    }

    return json.dumps({
        "status": "call_ending",
        "message": "Say a brief goodbye to the user before the call ends.",
    })


def pop_pending_end() -> dict | None:
    """Return and clear any pending end-call signal."""
    global _pending_end
    end = _pending_end
    _pending_end = None
    return end
