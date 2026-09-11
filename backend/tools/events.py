"""
How a tool reaches the caller's screen.

Two tools have a side effect beyond their return value: `show_scheme_card` puts a
card up, `end_call` hangs up. Both need to send a UI event, and neither can be
allowed to know *how* — a voice session sends it over RTVI, `/chat` returns it in
the HTTP response, and a future channel will do something else again.

So they take a `Deliver` callback. That is the whole abstraction, and the reason
it exists is that its predecessor was a pair of module globals shared by every
concurrent caller in the process.
"""
from typing import Any, Awaitable, Callable

Deliver = Callable[[dict[str, Any]], Awaitable[None]]


class EventCollector:
    """A `Deliver` that keeps the events instead of sending them.

    Used by `/chat`, which has no live connection to push to — the events go back
    in the response body — and by tests that want to assert on them.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def first(self, event_type: str) -> dict[str, Any] | None:
        return next((e for e in self.events if e.get("type") == event_type), None)
