"""
show_scheme_card — push a rich card to the caller's screen.

The card is delivered through an injected `deliver` callback rather than a
module-level mailbox. v1 stashed the payload in a global that the pipeline drained
between sentences, which meant two simultaneous callers could receive each
other's cards; the callback is bound to one session, so that cannot happen.
Pipecat passes `rtvi.send_server_message`, `/chat` passes a collector that puts
the card in its own HTTP response.

The event name is `show_scheme_card` — the same name the frontend already
handles. v1's `/chat` path broadcast `show_card` instead, so REST-triggered cards
were silently dropped; one name now, on both paths.

The payload is still rendered in the v1 knowledge-base shape (`metadata.state`,
`eligibility{}`, `translations{lang}`) because reshaping the card is a P4 item —
see `services.schemes.card_payload`.
"""
from typing import Any

from services import schemes as svc
from services.resources import Resources

from tools.events import Deliver

CARD_EVENT = "show_scheme_card"


async def show_scheme_card(
    res: Resources,
    *,
    scheme_id: str,
    language: str = "en",
    deliver: Deliver | None = None,
) -> dict[str, Any]:
    scheme = await svc.card_payload(res, scheme_id)
    if scheme is None:
        return {"error": f"Scheme '{scheme_id}' not found."}

    if deliver is not None:
        await deliver({"type": CARD_EVENT, "scheme": scheme,
                       "language": language})

    return {
        "status": "card_shown",
        "scheme_name": scheme.get("scheme_name"),
        "message": "Scheme card has been displayed on the user's screen.",
    }
