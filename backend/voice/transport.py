"""
Which wire the audio travels on — the one place that knows.

The hosting decision is still open (see the plan's decisions table), so the
pipeline must not name a transport. It asks for one here and gets whatever
`TRANSPORT` says: `smallwebrtc` for a self-hosted box, `daily` or `livekit` for
a managed SFU. Swapping is a config change, not a code change.

Only the `smallwebrtc` extra is installed today. The other two import lazily
inside their branch, so a missing dependency surfaces as an instruction ("pip
install pipecat-ai[daily]") at the moment someone selects it, rather than as an
ImportError at startup for everyone who didn't.

`TransportParams` deliberately carries no VAD analyzer in Pipecat 1.9 — VAD moved
onto the user context aggregator. Look in `voice/pipeline.py` for it.
"""
from typing import Any

from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer

from config import settings


class TransportUnavailable(RuntimeError):
    """The selected transport cannot be built — always with the reason why."""


def _base_params() -> TransportParams:
    """Audio in and out, nothing else.

    No video: the card goes over the RTVI data channel, not a video track, and a
    camera track on a 3G Android phone is bandwidth this product cannot spend.
    """
    return TransportParams(audio_in_enabled=True, audio_out_enabled=True)


def ice_servers() -> list[IceServer]:
    """`settings.ice_servers` in the shape the request handler wants.

    These are the servers the *bot's* peer connection uses to gather candidates;
    without them it only ever offers host candidates, which is fine on localhost
    and fails across networks.

    Credentials are not expressible here — `IceServer` takes `username` and
    `credential` separately, and a URL-embedded password in an env var is a
    secret in a log line waiting to happen. Add coturn explicitly when it goes
    in.
    """
    return [IceServer(urls=url) for url in settings.ice_servers if url]


def create_transport(
    *,
    webrtc_connection: Any | None = None,
    room_url: str | None = None,
    token: str | None = None,
) -> BaseTransport:
    """Build the transport for one session.

    Args:
        webrtc_connection: the `SmallWebRTCConnection` the request handler made
            for this caller. Required for `smallwebrtc`, ignored otherwise.
        room_url: the room to join. Falls back to `DAILY_ROOM_URL` /
            `LIVEKIT_URL`; a real deployment mints a room per session instead.
        token: join credential for the hosted transports.
    """
    kind = settings.transport

    if kind == "smallwebrtc":
        if webrtc_connection is None:
            raise TransportUnavailable(
                "smallwebrtc needs the per-caller connection from "
                "SmallWebRTCRequestHandler — see the /api/offer route in main.py.")
        from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
        return SmallWebRTCTransport(webrtc_connection, params=_base_params())

    if kind == "daily":
        url = room_url or settings.daily_room_url
        if not url:
            raise TransportUnavailable(
                "TRANSPORT=daily but no room: set DAILY_ROOM_URL, or pass "
                "room_url after creating a room with the Daily REST API.")
        if not settings.daily_api_key and not token:
            raise TransportUnavailable(
                "TRANSPORT=daily needs DAILY_API_KEY (to mint tokens) or an "
                "explicit token for this session.")
        try:
            from pipecat.transports.daily.transport import DailyParams, DailyTransport
        except ImportError as exc:
            raise TransportUnavailable(
                "TRANSPORT=daily requires: pip install 'pipecat-ai[daily]'") from exc
        return DailyTransport(
            url, token, "Bhasha Agent",
            params=DailyParams(audio_in_enabled=True, audio_out_enabled=True))

    if kind == "livekit":
        url = room_url or settings.livekit_url
        if not (url and token):
            raise TransportUnavailable(
                "TRANSPORT=livekit needs LIVEKIT_URL and a per-session join "
                "token signed with LIVEKIT_API_KEY/LIVEKIT_API_SECRET.")
        try:
            from pipecat.transports.livekit.transport import (
                LiveKitParams,
                LiveKitTransport,
            )
        except ImportError as exc:
            raise TransportUnavailable(
                "TRANSPORT=livekit requires: pip install 'pipecat-ai[livekit]'") from exc
        return LiveKitTransport(
            url=url, token=token, room_name="bhasha",
            params=LiveKitParams(audio_in_enabled=True, audio_out_enabled=True))

    raise TransportUnavailable(f"Unknown TRANSPORT: {kind!r}")
