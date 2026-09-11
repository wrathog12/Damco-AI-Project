"""
Two simultaneous callers — the P2 verification gate.

The specific thing v1 got wrong: `make_voice_handler()` was called once at
import, so `conversation_history` was process-global, and `_active_ws` was a flat
broadcast list. Two people calling at the same time shared one conversation and
each received the other's cards. The plan's P2 gate is therefore not "voice
works" but "two callers are independent".

This script connects two real WebRTC peers to a running server, speaks a
*different* question into each, and then checks that neither caller's stream ever
mentions the other caller's state. It needs Cartesia (to synthesise the two
utterances), Deepgram and Groq — it is a live end-to-end test, not a unit test.

    python main.py                        # in one shell
    python scripts/two_callers.py         # in another

Exit code 0 = independent. Non-zero = cross-talk, printed with the evidence.
"""
import argparse
import asyncio
import json
import sys
import time
from fractions import Fraction
from pathlib import Path

import av
import httpx
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

sys.stdout.reconfigure(encoding="utf-8")

SAMPLE_RATE = 24000
FRAME_SAMPLES = 480          # 20 ms, the pacing WebRTC expects

# Two callers, two states. The states are the tracer dye: if "Maharashtra" ever
# appears in caller A's stream, the sessions are not independent.
CALLERS = [
    {"name": "A", "state": "Bihar", "other": "Maharashtra",
     "utterance": "Bihar mein education scheme dikhao"},
    {"name": "B", "state": "Maharashtra", "other": "Bihar",
     "utterance": "Maharashtra mein kisan ke liye scheme dikhao"},
]


class SpeechTrack(MediaStreamTrack):
    """Silence until `gate` opens, then one utterance, then silence again.

    Paced against the wall clock — sending faster would hand the server an
    utterance whose pauses have been compressed away, and its VAD reads pauses.

    The gate is what makes the test deterministic. A fixed lead-in cannot work:
    the bot greets on connect, speaking over the greeting would test barge-in
    instead of the conversation, and the greeting starts whenever the pipeline
    happens to be ready. So the caller waits to be spoken to first, like a person.
    """

    kind = "audio"

    def __init__(self, speech: np.ndarray, gate: asyncio.Event, *,
                 pause_secs: float = 0.8) -> None:
        super().__init__()
        self._speech = speech.astype(np.int16)
        self._gate = gate
        self._pause_frames = int(pause_secs * SAMPLE_RATE / FRAME_SAMPLES)
        self._pos: int | None = None      # None until the gate opens
        self._pts = 0
        self._started: float | None = None
        self.spoke_at: float | None = None

    def _next_chunk(self) -> np.ndarray:
        if self._pos is None:
            if not self._gate.is_set():
                return np.zeros(FRAME_SAMPLES, np.int16)
            # A beat after the bot stops, so its trailing audio and ours don't
            # overlap in the server's VAD window.
            if self._pause_frames > 0:
                self._pause_frames -= 1
                return np.zeros(FRAME_SAMPLES, np.int16)
            self._pos = 0
            self.spoke_at = time.time()

        chunk = self._speech[self._pos:self._pos + FRAME_SAMPLES]
        self._pos += FRAME_SAMPLES
        if len(chunk) < FRAME_SAMPLES:
            chunk = np.pad(chunk, (0, FRAME_SAMPLES - len(chunk)))
        return chunk

    async def recv(self) -> av.AudioFrame:
        if self._started is None:
            self._started = time.time()

        target = self._started + self._pts / SAMPLE_RATE
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(self._next_chunk().tobytes())
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        self._pts += FRAME_SAMPLES
        return frame


class Caller:
    """One browser's worth of behaviour: send audio, collect what comes back."""

    def __init__(self, spec: dict, base_url: str) -> None:
        self.spec = spec
        self.base_url = base_url
        self.messages: list[dict] = []
        self.audio_ms = 0.0
        self.pc = RTCPeerConnection()
        self.greeted = asyncio.Event()     # opens when the bot stops speaking
        self.track: SpeechTrack | None = None
        self.replied_at: float | None = None
        # Everything the bot said *after* the greeting. Counting
        # `bot-stopped-speaking` events alone cannot tell an answer from a
        # tool-only turn, and a reply that arrives as several TTS segments fires
        # the event more than once — so the answer is "spoken words we did not
        # already have, followed by the bot going quiet".
        self.reply_parts: list[str] = []

    async def connect(self, speech: np.ndarray) -> None:
        self.track = SpeechTrack(speech, self.greeted)
        self.pc.addTrack(self.track)
        self.pc.addTransceiver("audio", direction="recvonly")

        channel = self.pc.createDataChannel("chat")

        @channel.on("open")
        def _on_open():
            # The RTVI handshake. Without it the processor never marks the bot
            # ready, and a real client would sit waiting.
            channel.send(json.dumps({
                "label": "rtvi-ai", "type": "client-ready", "id": "1",
                "data": {"version": "1.0.0"},
            }))

        @channel.on("message")
        def _on_message(raw):
            try:
                msg = json.loads(raw)
            except ValueError:
                self.messages.append({"type": "unparseable", "raw": str(raw)[:200]})
                return
            self.messages.append(msg)

            if (msg.get("type") == "bot-tts-text" and self.greeted.is_set()
                    and isinstance(msg.get("data"), dict)):
                text = (msg["data"].get("text") or "").strip()
                if text:
                    self.reply_parts.append(text)

            if msg.get("type") == "bot-stopped-speaking":
                if not self.greeted.is_set():
                    self.greeted.set()          # greeting over — our turn
                elif self.replied_at is None and self.reply_parts:
                    self.replied_at = time.time()

        @self.pc.on("track")
        def _on_track(track):
            async def drain():
                try:
                    while True:
                        frame = await track.recv()
                        self.audio_ms += 1000 * frame.samples / frame.sample_rate
                except Exception:
                    pass
            asyncio.create_task(drain())

        await self.pc.setLocalDescription(await self.pc.createOffer())
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/api/offer",
                json={"sdp": self.pc.localDescription.sdp,
                      "type": self.pc.localDescription.type})
            resp.raise_for_status()
            answer = resp.json()
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

    async def close(self) -> None:
        await self.pc.close()

    # ── what came back ──────────────────────────────────
    def text(self) -> str:
        """Every string anywhere in the received messages, concatenated."""
        def walk(node) -> list[str]:
            if isinstance(node, str):
                return [node]
            if isinstance(node, dict):
                return [s for v in node.values() for s in walk(v)]
            if isinstance(node, list):
                return [s for v in node for s in walk(v)]
            return []
        return " ".join(walk(self.messages))

    def cards(self) -> list[dict]:
        """`send_server_message(event)` arrives as `{type: server-message, data: event}`."""
        return [m["data"] for m in self.messages
                if m.get("type") == "server-message"
                and isinstance(m.get("data"), dict)
                and m["data"].get("type") == "show_scheme_card"]

    def spoken(self) -> str:
        """What the bot said, reassembled from its TTS text messages."""
        parts = [m["data"].get("text", "") for m in self.messages
                 if m.get("type") == "bot-tts-text"
                 and isinstance(m.get("data"), dict)]
        return "".join(parts)

    def reply(self) -> str:
        """Just the answer — the greeting is fixed text and proves nothing."""
        return " ".join(self.reply_parts)


async def run(base_url: str, duration: float) -> int:
    from voice import tts

    print("synthesising the two utterances")
    speech = {}
    for spec in CALLERS:
        speech[spec["name"]] = tts.synthesize(spec["utterance"], SAMPLE_RATE)
        print(f"  {spec['name']}: {spec['utterance']!r} "
              f"({len(speech[spec['name']]) / SAMPLE_RATE:.1f}s)")

    callers = [Caller(spec, base_url) for spec in CALLERS]
    print("\nconnecting both callers at once")
    t0 = time.time()
    await asyncio.gather(*(c.connect(speech[c.spec["name"]]) for c in callers))

    # Both greetings, then both answers. Waiting on events rather than sleeping
    # keeps the test honest about *what* it observed, and the timeout is the only
    # thing that turns a hung pipeline into a failure instead of a short report.
    try:
        await asyncio.wait_for(
            asyncio.gather(*(c.greeted.wait() for c in callers)), duration)
        print(f"  both greeted at +{time.time() - t0:.1f}s — speaking now")
    except asyncio.TimeoutError:
        print(f"  !! no greeting within {duration:.0f}s")

    remaining = max(5.0, duration - (time.time() - t0))
    try:
        await asyncio.wait_for(
            asyncio.gather(*(_await_reply(c) for c in callers)), remaining)
    except asyncio.TimeoutError:
        print("  !! not every caller got an answer in time")

    await asyncio.gather(*(c.close() for c in callers))

    print(f"\n{'=' * 70}")
    failures = 0
    for c in callers:
        spec = c.spec
        blob = c.text()
        leaked = blob.count(spec["other"])
        types = sorted({m.get("type", "?") for m in c.messages})
        turnaround = (c.replied_at - c.track.spoke_at
                      if c.replied_at and c.track and c.track.spoke_at else None)

        print(f"\ncaller {spec['name']} — said {spec['utterance']!r}")
        print(f"  audio received      {c.audio_ms / 1000:.1f}s")
        print(f"  rtvi messages       {len(c.messages)}")
        print(f"    types             {', '.join(types)}")
        print(f"  cards               {len(c.cards())}"
              + (f"  -> {c.cards()[0]['scheme'].get('scheme_name')}"
                 if c.cards() else ""))
        print(f"  answer turnaround   "
              + (f"{turnaround:.1f}s" if turnaround else "(no answer)"))
        print(f"  mentions of {spec['other']:<12} {leaked}")
        print(f"  bot answered: {c.reply()[:400] or '(nothing)'}")

        if c.audio_ms < 1000:
            print("  >> FAIL: no audio came back — the bot never spoke")
            failures += 1
        if c.replied_at is None:
            print("  >> FAIL: the bot never answered the question")
            failures += 1
        if leaked:
            print(f"  >> FAIL: {spec['other']} appeared in {spec['name']}'s "
                  f"stream — the sessions are not independent")
            failures += 1

    print(f"\n{'=' * 70}\nfailures: {failures}")
    return 1 if failures else 0


async def _await_reply(caller: Caller) -> None:
    while caller.replied_at is None:
        await asyncio.sleep(0.2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--seconds", type=float, default=90.0,
                   help="budget for the greeting, and again for the answer")
    args = p.parse_args()
    return asyncio.run(run(args.url, args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
