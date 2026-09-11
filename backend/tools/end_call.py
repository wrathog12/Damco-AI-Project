"""
end_call — tell the caller's client the conversation is over.

Only the event is sent here. Tearing the pipeline down is the pipeline's job, and
it has to wait until the goodbye has actually been spoken — see
`voice/pipeline.py`, which ends the session on `on_bot_stopped_speaking` after
this fires. Ending it from inside the handler would cut the goodbye off
mid-word.
"""
from typing import Any

from tools.events import Deliver

END_EVENT = "end_call"


async def end_call(*, reason: str = "user_requested",
                   deliver: Deliver | None = None) -> dict[str, Any]:
    if deliver is not None:
        await deliver({"type": END_EVENT, "reason": reason})

    return {
        "status": "call_ending",
        "message": "Say a brief goodbye to the user before the call ends.",
    }
