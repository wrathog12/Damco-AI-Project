"""
Turn quota and anonymous identity — the P3 slice-1 verification gate.

Two things have to hold, and they fail in different ways:

1. **The allowance is per identity and is actually spent.** A caller gets exactly
   `limit` turns, the next one is refused, and refusing does not charge for the
   refusal. Two identities do not share a bucket.
2. **A refusal on a live call is spoken, not thrown.** The agent says the turns
   are gone and the call closes itself once the sentence is out. A caller must
   never be dropped mid-word, and must never get a 401.

The HTTP half needs only `python main.py` running. The voice half (`--voice`) also
synthesises speech with Cartesia and pushes it over aiortc, so it needs the same
keys `two_callers.py` does — and it is slow, one real spoken turn at a time. Run
the server with a small allowance for it:

    ANON_TURN_QUOTA=2 python main.py     # in one shell
    python scripts/verify_quota.py       # in another
    python scripts/verify_quota.py --voice

Exit code 0 = both halves clean.
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
COOKIE = "bh_anon"

# One question per turn. Different every time, because repeating a question is a
# turn the model may answer from context without searching — which still counts,
# but makes the transcript harder to read when something goes wrong.
QUESTIONS = [
    "Bihar mein education scheme dikhao",
    "Uski eligibility kya hai",
    "Koi doosri scheme batao",
    "Aur kya milega",
    "Kisan ke liye kuch hai",
]


def _fail(msg: str) -> int:
    print(f"  >> FAIL: {msg}")
    return 1


# ── HTTP: identity, metering, refusal ───────────────────
async def check_http(base_url: str) -> int:
    failures = 0
    print("=" * 70)
    print("HTTP: identity and metering")
    print("=" * 70)

    # A cookie jar per client is the whole point: it is what makes this client one
    # anonymous caller rather than a new one on every request.
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        # ── identity is minted, not demanded ────────────────
        resp = await client.get("/api/quota")
        if resp.status_code != 200:
            return _fail(f"GET /api/quota returned {resp.status_code}, not 200 — "
                         "an anonymous caller must never be refused")
        state = resp.json()
        cookie = client.cookies.get(COOKIE)
        limit = state["limit"]

        print(f"  first contact       used={state['used']}/{limit} "
              f"auth={state['authenticated']}")
        print(f"  cookie {COOKIE}      {'set' if cookie else 'MISSING'}")
        if not cookie:
            failures += _fail(f"no {COOKIE} cookie — every request would be a new "
                              "identity and the quota would never bind")
        if state["used"] != 0:
            failures += _fail(f"a fresh identity starts at used={state['used']}")

        # ── the cookie is signed ────────────────────────────
        # Tampering must not be accepted *as an identity*. It cannot be prevented
        # from getting a new one — deleting the cookie does that too — so what is
        # checked is that the server replaced the value rather than trusting it.
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as forger:
            forged = "11111111-1111-4111-8111-111111111111.deadbeef"
            forger.cookies.set(COOKIE, forged)
            # Read the *response's* own jar, not the client's: the forged value
            # was set without a domain and the server's replacement carries one,
            # so the client holds both and `cookies.get` refuses to choose.
            issued = (await forger.get("/api/quota")).cookies.get(COOKIE)
            ok = issued is not None and issued != forged
            print(f"  forged cookie       "
                  f"{'rejected, new identity issued' if ok else 'ACCEPTED'}")
            if not ok:
                failures += _fail("a cookie with a bad signature was accepted — "
                                  "anyone could mint unlimited free turns")

        # ── peek does not spend ─────────────────────────────
        await client.get("/api/quota")
        again = (await client.get("/api/quota")).json()
        print(f"  after 3 peeks       used={again['used']}/{limit}")
        if again["used"] != 0:
            failures += _fail("GET /api/quota consumed a turn — it must only read")

        # ── spend the allowance ─────────────────────────────
        print(f"  spending {limit} turns through POST /chat "
              f"(one real LLM call each)")
        for turn in range(1, limit + 1):
            body = (await client.post("/chat", json={
                "message": QUESTIONS[(turn - 1) % len(QUESTIONS)],
                "language": "hi",
            })).json()
            q = body.get("quota") or {}
            print(f"    turn {turn:>2}          used={q.get('used')}/{q.get('limit')} "
                  f"remaining={q.get('remaining')} allowed={q.get('allowed')}")
            if not q.get("allowed"):
                failures += _fail(f"turn {turn} of {limit} was refused")
                break
            if q.get("used") != turn:
                failures += _fail(f"turn {turn} reported used={q.get('used')}")

        # ── one past the end ───────────────────────────────
        resp = await client.post("/chat", json={"message": QUESTIONS[0],
                                               "language": "hi"})
        body = resp.json()
        q = body.get("quota") or {}
        print(f"  turn {limit + 1:>2} (over)     status={resp.status_code} "
              f"allowed={q.get('allowed')} used={q.get('used')}")
        print(f"    said: {body.get('response', '')[:100]}")
        if resp.status_code != 200:
            failures += _fail(f"a refusal came back as {resp.status_code}; it must "
                              "be a 200 the client renders as a reply")
        if q.get("allowed") is not False:
            failures += _fail("the turn past the limit was allowed")
        if q.get("used") != limit:
            failures += _fail(f"the refused turn was charged: used={q.get('used')} "
                              f"with limit={limit}")
        if not body.get("response"):
            failures += _fail("a refusal with no message — the caller is told nothing")

        # ── another identity is untouched ──────────────────
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as other:
            fresh = (await other.get("/api/quota")).json()
            print(f"  second identity     used={fresh['used']}/{fresh['limit']}")
            if fresh["used"] != 0:
                failures += _fail("a second caller inherited the first one's "
                                  "usage — the quota is not per identity")

    return failures


# ── Voice: refusal is spoken, then the call closes ──────
class TurnTrack(MediaStreamTrack):
    """Silence, then one utterance each time `turn` is opened.

    Same wall-clock pacing as `two_callers.py`'s track and for the same reason —
    the server's VAD reads pauses, so an utterance sent faster than real time has
    had its pauses compressed away. The difference here is that it speaks more
    than once: the point of this test is the turn *after* the last allowed one.
    """

    kind = "audio"

    def __init__(self, speeches: list[np.ndarray], turn: asyncio.Event, *,
                 pause_secs: float = 0.8) -> None:
        super().__init__()
        self._speeches = [s.astype(np.int16) for s in speeches]
        self._turn = turn
        self._pause_secs = pause_secs
        self._pause_frames = 0
        self._pos: int | None = None
        self._index = 0
        self._pts = 0
        self._started: float | None = None
        self.spoken = 0

    def _next_chunk(self) -> np.ndarray:
        if self._pos is None:
            if not self._turn.is_set() or self._index >= len(self._speeches):
                return np.zeros(FRAME_SAMPLES, np.int16)
            if self._pause_frames > 0:
                self._pause_frames -= 1
                return np.zeros(FRAME_SAMPLES, np.int16)
            self._pos = 0

        speech = self._speeches[self._index]
        chunk = speech[self._pos:self._pos + FRAME_SAMPLES]
        self._pos += FRAME_SAMPLES
        if len(chunk) < FRAME_SAMPLES:
            chunk = np.pad(chunk, (0, FRAME_SAMPLES - len(chunk)))
            # Utterance over: close the gate behind us so the next one waits for
            # the bot to finish rather than talking over its answer.
            self._pos = None
            self._index += 1
            self._pause_frames = int(self._pause_secs * SAMPLE_RATE / FRAME_SAMPLES)
            self._turn.clear()
            self.spoken += 1
        return chunk

    async def recv(self) -> av.AudioFrame:
        if self._started is None:
            self._started = time.time()
        delay = self._started + self._pts / SAMPLE_RATE - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(self._next_chunk().tobytes())
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        self._pts += FRAME_SAMPLES
        return frame


class VoiceCaller:
    """One caller who keeps talking until the server stops them."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.pc = RTCPeerConnection()
        self.turn = asyncio.Event()
        self.track: TurnTrack | None = None
        self.replies: list[str] = []     # one entry per bot turn, greeting first
        self._parts: list[str] = []
        self.audio_ms = 0.0
        self.closed = asyncio.Event()

    async def connect(self, speeches: list[np.ndarray]) -> None:
        self.track = TurnTrack(speeches, self.turn)
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
            data = msg.get("data") if isinstance(msg.get("data"), dict) else {}

            if msg.get("type") == "bot-tts-text":
                text = (data.get("text") or "").strip()
                if text:
                    self._parts.append(text)
            elif msg.get("type") == "bot-stopped-speaking":
                # A reply can arrive as several TTS segments, so a bot turn is
                # "the words since it last went quiet".
                if self._parts:
                    self.replies.append(" ".join(self._parts))
                    self._parts = []
                self.turn.set()          # our move

        @self.pc.on("connectionstatechange")
        def _on_state():
            if self.pc.connectionState in {"closed", "failed", "disconnected"}:
                self.closed.set()

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
        resp = await self.client.post("/api/offer", json={
            "sdp": self.pc.localDescription.sdp,
            "type": self.pc.localDescription.type})
        resp.raise_for_status()
        answer = resp.json()
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))


async def check_voice(base_url: str, budget: float) -> int:
    from voice import tts

    failures = 0
    print("\n" + "=" * 70)
    print("VOICE: the refusal is spoken and the call closes itself")
    print("=" * 70)

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        limit = (await client.get("/api/quota")).json()["limit"]
        if limit > 3:
            print(f"  SKIP: the allowance is {limit} turns, which is "
                  f"{limit + 1} spoken turns of real audio. Restart the server "
                  f"with ANON_TURN_QUOTA=2 to run this.")
            return 0

        # limit + 1 utterances: the last one is the one that must be refused.
        print(f"  synthesising {limit + 1} utterances")
        speeches = [tts.synthesize(QUESTIONS[i % len(QUESTIONS)], SAMPLE_RATE)
                    for i in range(limit + 1)]

        caller = VoiceCaller(client)
        await caller.connect(speeches)
        print("  connected; waiting for the greeting, then speaking each turn")

        # The bot's own turns are the clock: `turn` opens on every
        # bot-stopped-speaking, and the track speaks once per opening.
        deadline = time.time() + budget
        while time.time() < deadline:
            if caller.track.spoken > limit and (
                    len(caller.replies) >= limit + 2 or caller.closed.is_set()):
                break
            await asyncio.sleep(0.3)

        # The pipeline should end itself once the refusal has been spoken. Give it
        # a moment before concluding it did not.
        try:
            await asyncio.wait_for(caller.closed.wait(), 15)
        except asyncio.TimeoutError:
            pass

        closed_by_server = caller.closed.is_set()
        await caller.pc.close()

        greeting, *turns = caller.replies or [""]
        print(f"\n  audio received      {caller.audio_ms / 1000:.1f}s")
        print(f"  utterances spoken   {caller.track.spoken} of {limit + 1}")
        print(f"  bot turns           {len(turns)} after the greeting")
        for i, reply in enumerate(turns, 1):
            tag = "refusal?" if i > limit else f"turn {i}"
            print(f"    {tag:<10} {reply[:150]}")
        print(f"  server closed call  {closed_by_server}")

        final = (await client.get("/api/quota")).json()
        print(f"  final allowance     used={final['used']}/{final['limit']}")

        if caller.audio_ms < 1000:
            failures += _fail("no audio came back — the bot never spoke")
        if caller.track.spoken <= limit:
            failures += _fail(f"only {caller.track.spoken} of {limit + 1} "
                              "utterances were spoken; the bot stopped replying "
                              "before the allowance ran out")
        if len(turns) < limit + 1:
            failures += _fail(f"{len(turns)} bot turns for {limit + 1} questions — "
                              "the refused turn produced no spoken reply, so the "
                              "caller was cut off with no explanation")
        if final["used"] != limit:
            failures += _fail(f"used={final['used']} against limit={limit}: the "
                              "refused turn was charged, or a turn was not")
        if not closed_by_server:
            failures += _fail("the call was still open after the refusal — the "
                              "caller is holding a line they cannot use")

    return failures


async def run(base_url: str, voice: bool, budget: float) -> int:
    failures = await check_http(base_url)
    if voice:
        failures += await check_voice(base_url, budget)
    print(f"\n{'=' * 70}\nfailures: {failures}")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--voice", action="store_true",
                   help="also run a live call (needs a small ANON_TURN_QUOTA)")
    p.add_argument("--seconds", type=float, default=180.0,
                   help="budget for the whole spoken conversation")
    args = p.parse_args()
    return asyncio.run(run(args.url, args.voice, args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
