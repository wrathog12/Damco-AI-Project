"""
Barge-in — the other half of the P2 gate.

`scripts/two_callers.py` waits politely for the greeting to finish, which is the
right shape for testing session independence but deliberately avoids the case
that matters most in a real call: the caller talks *over* the bot. v1 got this
for free from FastRTC's `ReplyOnPause(can_interrupt=True)` abandoning the
generator mid-yield; in Pipecat it has to come from the VAD on the user
aggregator raising an interruption that stops TTS and clears the output buffer.
Nothing verifies that, so this does.

One caller. It waits for the bot to *start* the greeting, cuts in after a beat,
and then checks three things:

  * the bot stopped speaking soon after we started — it did not talk over us;
  * an interruption was actually signalled to the client;
  * the answer we get back is to *our* question, not the tail of the greeting.

It also reports connect → first-audio, because that number is the first thing a
caller experiences and the two-caller harness can only measure it with two
sessions competing.

    python main.py                    # in one shell
    python scripts/barge_in.py        # in another
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

sys.stdout.reconfigure(encoding="utf-8")

from scripts.two_callers import SAMPLE_RATE, SpeechTrack  # noqa: E402

UTTERANCE = "Bihar mein education scheme dikhao"
# Long enough that the bot is unmistakably mid-sentence — the greeting runs ~10s,
# so cutting in here lands in the middle of it rather than near its end.
CUT_IN_AFTER = 2.0


class Barger:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.pc = RTCPeerConnection()
        self.messages: list[dict] = []
        self.gate = asyncio.Event()
        self.track: SpeechTrack | None = None
        self.audio_ms = 0.0
        self.t_connect: float | None = None
        self.t_first_audio: float | None = None
        self.t_bot_started: float | None = None
        self.t_bot_stopped: float | None = None   # first stop after we cut in
        self.t_interrupted: float | None = None
        self.reply_parts: list[str] = []

    async def connect(self, speech) -> None:
        # pause_secs=0: the point is to overlap, not to wait for a gap.
        self.track = SpeechTrack(speech, self.gate, pause_secs=0.0)
        self.pc.addTrack(self.track)
        self.pc.addTransceiver("audio", direction="recvonly")

        channel = self.pc.createDataChannel("chat")

        @channel.on("open")
        def _on_open():
            channel.send(json.dumps({
                "label": "rtvi-ai", "type": "client-ready", "id": "1",
                "data": {"version": "1.0.0"},
            }))

        @channel.on("message")
        def _on_message(raw):
            try:
                msg = json.loads(raw)
            except ValueError:
                return
            self.messages.append(msg)
            kind = msg.get("type")
            now = time.time()

            if kind == "bot-started-speaking" and self.t_bot_started is None:
                self.t_bot_started = now
                # Cut in mid-greeting, from a task so the handler stays cheap.
                asyncio.create_task(self._cut_in())

            # Only interruptions that follow our speech are evidence; the
            # aggregator also emits one when a turn opens normally.
            spoke_at = self.track.spoke_at if self.track else None
            if kind == "bot-interrupted" and spoke_at and self.t_interrupted is None:
                self.t_interrupted = now
            if (kind == "bot-stopped-speaking" and spoke_at
                    and self.t_bot_stopped is None):
                self.t_bot_stopped = now
            # Gated on the barge-in having landed, not merely on us having
            # started talking: the greeting's remaining TTS text frames are
            # still in flight when we cut in, and counting those made "the bot
            # answered" true for a run whose only reply was the tail of "…your
            # voice assistant".
            if (kind == "bot-tts-text" and self.t_bot_stopped
                    and isinstance(msg.get("data"), dict)):
                text = (msg["data"].get("text") or "").strip()
                if text:
                    self.reply_parts.append(text)

        @self.pc.on("track")
        def _on_track(track):
            async def drain():
                try:
                    while True:
                        frame = await track.recv()
                        if self.t_first_audio is None:
                            self.t_first_audio = time.time()
                        self.audio_ms += 1000 * frame.samples / frame.sample_rate
                except Exception:
                    pass
            asyncio.create_task(drain())

        self.t_connect = time.time()
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

    async def _cut_in(self) -> None:
        await asyncio.sleep(CUT_IN_AFTER)
        self.gate.set()

    def reply(self) -> str:
        return " ".join(self.reply_parts)


async def run(base_url: str, budget: float) -> int:
    from voice import tts

    print(f"synthesising {UTTERANCE!r}")
    speech = tts.synthesize(UTTERANCE, SAMPLE_RATE)

    b = Barger(base_url)
    await b.connect(speech)

    deadline = time.time() + budget
    while time.time() < deadline:
        # Done once the bot has answered us: it stopped for our barge-in and
        # then said something new.
        if b.t_bot_stopped and b.reply_parts:
            await asyncio.sleep(3.0)      # let the tail of the answer arrive
            break
        await asyncio.sleep(0.2)
    await b.pc.close()

    spoke_at = b.track.spoke_at if b.track else None
    print(f"\n{'=' * 70}")
    print(f"  connect -> first audio    "
          + (f"{b.t_first_audio - b.t_connect:.1f}s"
             if b.t_first_audio and b.t_connect else "(no audio)"))
    print(f"  greeting started at       "
          + (f"+{b.t_bot_started - b.t_connect:.1f}s"
             if b.t_bot_started and b.t_connect else "(never)"))
    print(f"  we cut in at              "
          + (f"+{spoke_at - b.t_connect:.1f}s" if spoke_at and b.t_connect
             else "(never spoke)"))
    print(f"  bot went quiet after      "
          + (f"{b.t_bot_stopped - spoke_at:.2f}s of us talking"
             if b.t_bot_stopped and spoke_at else "(never)"))
    print(f"  interruption signalled    "
          + (f"yes, {b.t_interrupted - spoke_at:.2f}s in"
             if b.t_interrupted and spoke_at else "no"))
    print(f"  audio received            {b.audio_ms / 1000:.1f}s")
    print(f"  bot answered: {b.reply()[:400] or '(nothing)'}")

    failures = 0
    if not spoke_at:
        print("  >> FAIL: the greeting never started, so we never barged in")
        failures += 1
    elif b.t_bot_stopped is None:
        print("  >> FAIL: the bot talked straight through us — no barge-in")
        failures += 1
    elif b.t_bot_stopped - spoke_at > 2.0:
        print(f"  >> FAIL: took {b.t_bot_stopped - spoke_at:.1f}s to stop; a "
              f"caller experiences that as being ignored")
        failures += 1
    if spoke_at and not b.reply_parts:
        print("  >> FAIL: interrupted, but never answered the question we asked")
        failures += 1
    if b.reply() and "Maharashtra" in b.reply():
        print("  >> FAIL: answered about the wrong state")
        failures += 1

    print(f"\n{'=' * 70}\nfailures: {failures}")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--seconds", type=float, default=90.0)
    args = p.parse_args()
    return asyncio.run(run(args.url, args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
