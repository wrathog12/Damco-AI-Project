"""
One voice session = one Pipecat pipeline.

This replaces the FastRTC synchronous generator, and with it every workaround it
needed. Worth being explicit about what is gone, because each one was a real bug:

* **Process-global `conversation_history`.** `make_voice_handler()` was called
  once at import, so two callers shared one conversation. Now `run_session()` is
  called per connection and the `LLMContext` it builds belongs to that caller.
* **`_push_ws_event` + `run_coroutine_threadsafe(...).result(timeout=3.0)`.**
  The generator ran on a worker thread and blocked up to three seconds per UI
  push. Everything here is async on the server's own loop.
* **The `/ws/cards` broadcast list.** UI events now travel over RTVI on the same
  transport as the audio, addressed to one client.
* **`_pending_card` / `_pending_end` module globals.** The tools take a
  `deliver` callback; see `tools/events.py`.
* **`_inactivity_watcher`.** `PipelineWorker(idle_timeout_secs=...)` raises
  `on_idle_timeout` itself.
* **`prompts.build_messages`'s `[-10:]` truncation.** Context summarisation
  compresses old turns instead of dropping them, so the agent stops forgetting
  the state you told it four turns ago.

The pipeline:

    transport.input → STT → LanguageTagger → [tap] → user aggregator → QuotaGate
                    → LLM → TTS → transport.output → CallCloser → [tap]
                    → assistant aggregator

The RTVI processor is prepended by `PipelineWorker` itself (`enable_rtvi`), so
`worker.rtvi.send_server_message(...)` reaches the client without being wired in
here.

The two `[tap]`s are the transcript recorder, and they are only in the pipeline
when there is something to record — see `TranscriptRecorder` for why there are two
of them and `services/conversations.py` for who may be recorded at all. A session
also opens with the caller's profile injected ahead of the few-shots, which is what
stops the agent asking a returning caller which state they are from.
"""
import asyncio
import uuid
from typing import Any

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndWorkerFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService, CartesiaTTSSettings
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.google.llm import GoogleLLMService, GoogleLLMSettings
from pipecat.workers.runner import WorkerRunner

from config import settings as cfg
from models.identity import User
from services import conversations as conv_service
from services import profiles as profile_service
from services import quota
from services.resources import Resources
from tools.end_call import END_EVENT
from tools.registry import TOOL_SCHEMAS, ToolContext
from voice.prompts import (
    GREETING,
    SYSTEM_PROMPT,
    quota_exhausted_greeting,
    seed_messages,
)
from voice.quota_gate import QuotaGate
from voice.transport import create_transport

# Deepgram's language code → the name to put in front of the user's words.
# Romanised Hinglish is why this exists: "mujhe scholarship chahiye" in Latin
# script is genuinely ambiguous to the model, and answering a Hindi speaker in
# English is the complaint this system gets most.
_LANGUAGE_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "bn": "Bengali",
    "mr": "Marathi",
    "ta": "Tamil",
    "te": "Telugu",
    "gu": "Gujarati",
    "kn": "Kannada",
    "ml": "Malayalam",
    "pa": "Punjabi",
}

# RTVI server-message type for the turn allowance. Named here, next to the only
# place that sends it, the way CARD_EVENT and END_EVENT live next to their tools.
QUOTA_EVENT = "quota"

# Languages the Cartesia voice can be switched to mid-call. Anything else keeps
# the current setting rather than sending Cartesia a code it will reject.
_TTS_LANGUAGES = {"en", "hi", "bn", "mr", "gu", "ta", "te", "kn", "ml", "pl"}


def _language_code(language: Any) -> str | None:
    """`Language.HI` / `"hi-IN"` / `None` → `"hi"` / `None`."""
    code = getattr(language, "value", language)
    if not code:
        return None
    return str(code).split("-")[0].lower()


class LanguageTagger(FrameProcessor):
    """Tells the LLM — and Cartesia — which language the caller just used.

    Deepgram Nova-3's `multi` mode reports a language per utterance; that report
    is the only language signal in the system, and v1 spent it by pasting
    `[User is speaking Hindi]` onto the front of the user's transcript. That
    worked but put scaffolding inside the conversation: the text went into
    context, into the client's transcript display, and into every later turn.

    Here the tag is a separate one-line context message, emitted only when the
    language *changes*, and the transcript itself is left alone. It also retunes
    the TTS voice on the same signal, because a Bengali reply rendered with the
    Hindi language setting is intelligible but wrong-sounding.
    """

    def __init__(self) -> None:
        super().__init__()
        self._current: str | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            code = _language_code(frame.language)
            if code and code != self._current:
                self._current = code
                name = _LANGUAGE_NAMES.get(code)
                if name:
                    # run_llm=False: this only annotates the context. The user
                    # aggregator runs the LLM when the turn ends, and running it
                    # here would answer half an utterance.
                    await self.push_frame(LLMMessagesAppendFrame(
                        messages=[{
                            "role": "system",
                            "content": (f"The user is now speaking {name}. "
                                        f"Reply in {name}."),
                        }],
                        run_llm=False,
                    ), direction)
                if code in _TTS_LANGUAGES:
                    await self.push_frame(
                        TTSUpdateSettingsFrame(settings={"language": code}),
                        direction)

        await self.push_frame(frame, direction)


class CallCloser(FrameProcessor):
    """Ends the pipeline after the goodbye has actually been spoken.

    `end_call` only sends its event; if it ended the session itself the caller
    would hear the sentence cut off mid-word, because the LLM has not even
    generated the goodbye at the point the tool returns. So the tool arms this,
    and this waits for the bot to stop speaking.

    It sits after `transport.output()`, which is where `BotStoppedSpeakingFrame`
    is emitted from.
    """

    def __init__(self) -> None:
        super().__init__()
        self._armed = False
        self._ending = False

    def arm(self) -> None:
        self._armed = True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        if (self._armed and not self._ending
                and isinstance(frame, BotStoppedSpeakingFrame)):
            self._ending = True
            logger.info("end_call: goodbye finished, closing the pipeline")
            await self.push_frame(EndWorkerFrame(reason="end_call"), direction)


class TranscriptRecorder:
    """Collects the call's transcript, in order, for `services/conversations.py`.

    **Hand-rolled deliberately.** Pipecat 1.9 has no `TranscriptProcessor` —
    `pipecat.processors.transcript_processor` does not exist and nothing in the
    installed package emits a `TranscriptionMessage` or an `on_transcript_update`
    event. So this joins `LanguageTagger`, `CallCloser` and `QuotaGate` as local
    logic, and it is written to survive the frames it does not understand.

    Two sources, because no single point in the pipeline sees both sides:

    * The **user's** words come from `TranscriptionFrame`, which the user
      aggregator *consumes* rather than forwards — verified in
      `llm_response_universal.py`, where interim and final transcriptions are
      explicitly not pushed downstream. So that half has to be observed before the
      aggregator.
    * The **agent's** words come from `LLMTextFrame` between
      `LLMFullResponseStartFrame` and `LLMFullResponseEndFrame`, downstream of the
      LLM. Not `TTSTextFrame`: with a service that reports word timestamps —
      Cartesia does — those arrive one word at a time with their own spacing
      rules, so reassembling a sentence from them is fiddly for no gain.

    Hence a recorder plus two `TranscriptTap`s rather than one processor.

    Two honest limitations, both accepted:

    * A reply the caller **barges in on** is recorded in full, because this
      observes what the model produced rather than what the speaker finished
      saying. The alternative is reconstructing playback position from word
      timestamps, which is a lot of machinery to make a stored transcript
      marginally more accurate about a sentence nobody disputed.
    * Rows are written **once, at hang-up**. A process killed mid-call loses that
      call's transcript. That is the right trade here: the transcript is a
      convenience, while the things that actually change what the agent knows next
      time — the profile and the scheme interactions — are written eagerly, as
      they happen.
    """

    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.turn_count = 0
        self.language: str | None = None
        self._reply: list[str] = []
        self._in_reply = False

    def observe_user(self, frame: Frame) -> None:
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self.turns.append(("user", frame.text.strip()))
            self.turn_count += 1
            code = _language_code(frame.language)
            if code:
                # The last language heard, not the first: it is what the recap for
                # the *next* call should be written in.
                self.language = code

    def observe_bot(self, frame: Frame) -> None:
        if isinstance(frame, LLMFullResponseStartFrame):
            self._reply = []
            self._in_reply = True
        elif isinstance(frame, LLMTextFrame) and self._in_reply:
            self._reply.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._in_reply = False
            text = "".join(self._reply).strip()
            self._reply = []
            if text:
                # A turn that only called a tool produces no text, and a blank row
                # in a transcript reads as a pause that never happened.
                self.turns.append(("assistant", text))


class TranscriptTap(FrameProcessor):
    """A pass-through that shows every frame to a recorder and changes nothing.

    Separate from the recorder so the same recorder can be watched from two points
    in the pipeline, and so the observation can never alter the frame: this class
    has no branch that skips `push_frame`.
    """

    def __init__(self, observe) -> None:
        super().__init__()
        self._observe = observe

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        try:
            self._observe(frame)
        except Exception as exc:                                # noqa: BLE001
            # A recording bug must never break a live call.
            logger.warning(f"transcript tap: {type(exc).__name__}: {exc}")
        await self.push_frame(frame, direction)


def _build_services() -> tuple[DeepgramSTTService, GoogleLLMService, CartesiaTTSService]:
    stt = DeepgramSTTService(
        api_key=cfg.deepgram_api_key,
        settings=DeepgramSTTSettings(
            model=cfg.stt_model,
            # "multi" — code-switching. See config.stt_live_language.
            language=cfg.stt_live_language,
            interim_results=True,
            punctuate=True,
            smart_format=True,
            # See config: without these two, one utterance arrives as several
            # finals and the LLM answers the first fragment.
            endpointing=cfg.stt_endpointing_ms,
            utterance_end_ms=cfg.stt_utterance_end_ms,
        ),
    )
    llm = GoogleLLMService(
        api_key=cfg.gemini_api_key,
        settings=GoogleLLMSettings(
            model=cfg.llm_model,
            # The system prompt lives here, not in the message list, so context
            # summarisation cannot rewrite or drop it.
            system_instruction=SYSTEM_PROMPT,
            temperature=0.6,
            max_tokens=cfg.llm_max_tokens,
            # `thinking_level`, not gpt-oss's `reasoning_effort` — and a declared
            # settings field rather than something smuggled through `extra`.
            thinking=GoogleLLMService.ThinkingConfig(
                thinking_level=cfg.llm_thinking_level),
        ),
    )
    tts = CartesiaTTSService(
        api_key=cfg.cartesia_api_key,
        # `voice` on the settings, not the deprecated `voice_id=` argument.
        # `language` is the starting point only — LanguageTagger retunes it with
        # a TTSUpdateSettingsFrame when the caller switches language.
        settings=CartesiaTTSSettings(
            model=cfg.tts_model,
            voice=cfg.tts_voice_id,
            language="hi",
        ),
    )
    return stt, llm, tts


async def run_session(
    resources: Resources,
    *,
    user: User,
    webrtc_connection: Any | None = None,
    conversation_id: str | None = None,
) -> None:
    """Serve one caller from connect to hang-up. Returns when the call ends.

    `resources` is the process-wide pool handed in from the FastAPI lifespan —
    shared deliberately, because a connection pool per caller would exhaust
    Postgres. Everything else in here is built fresh per session.

    `user` is whoever `POST /api/offer` resolved — anonymous or authenticated,
    resolved there rather than here because that is the request that carries the
    cookie. It is a detached ORM row and is read, not refreshed: only `id` and
    `phone_e164` are used, and the live counter is read inside the same statement
    that increments it (see `services/quota.py`).
    """
    conversation_id = conversation_id or str(uuid.uuid4())
    log = logger.bind(call=conversation_id[:8])

    transport = create_transport(webrtc_connection=webrtc_connection)
    stt, llm, tts = _build_services()

    # The whole point of P3 slice 3: a returning caller's session starts already
    # knowing their state and age, so the agent stops asking. `session_context`
    # returns None for an anonymous caller, for one with no profile, and when no
    # encryption key is configured — in all three cases this is exactly the P2
    # pipeline, which is why nothing below is conditional on it.
    notice = await profile_service.session_context(resources, user)
    if notice:
        log.info(f"restored profile context for user {user.id}")
    persist = await conv_service.start(resources, user, channel="voice",
                                       conversation_id=conversation_id)
    recorder = TranscriptRecorder() if persist else None

    context = LLMContext(messages=seed_messages(notice), tools=TOOL_SCHEMAS)
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # In Pipecat 1.9 VAD belongs to the user aggregator, not the
            # transport — TransportParams has no vad_analyzer field.
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(stop_secs=cfg.vad_stop_secs)),
        ),
        assistant_params=LLMAssistantAggregatorParams(
            # Replaces the [-10:] truncation: old turns are compressed instead
            # of discarded, so the caller's state and age survive a long call.
            enable_auto_context_summarization=True,
        ),
    )

    tagger = LanguageTagger()
    closer = CallCloser()
    # The meter sits in front of the LLM, so a turn is counted where it is spent.
    # `closer.arm` is handed over rather than looked up: the gate ends the call the
    # same way `end_call` does, after the last sentence is actually spoken.
    gate = QuotaGate(resources=resources, user=user, on_exhausted=closer.arm)

    # `deliver` is filled in below — the callback needs the worker, and the
    # worker needs this object as its app_resources.
    #
    # `user` is what makes `check_eligibility` answerable from memory and what
    # attributes a card to the person who saw it; `conversation_id` is what ties an
    # interaction to the call it happened on.
    tool_ctx = ToolContext(resources=resources, user=user,
                           conversation_id=conversation_id)

    pipeline = Pipeline([
        transport.input(),
        stt,
        tagger,
        # Before the user aggregator, which consumes TranscriptionFrame rather than
        # forwarding it — see TranscriptRecorder.
        *([TranscriptTap(recorder.observe_user)] if recorder else []),
        aggregators.user(),
        gate,
        llm,
        tts,
        transport.output(),
        closer,
        # After the LLM, where the reply text is visible.
        *([TranscriptTap(recorder.observe_bot)] if recorder else []),
        aggregators.assistant(),
    ])

    worker = PipelineWorker(
        pipeline,
        app_resources=tool_ctx,
        conversation_id=conversation_id,
        idle_timeout_secs=cfg.idle_timeout_secs,
        # We hang up ourselves so the client is told why first.
        cancel_on_idle_timeout=False,
        # Pipecat's default is 20s, and one cold session here spends ~13s of it
        # connecting Deepgram and Cartesia and warming the lazy imports. Two
        # callers connecting at the same moment therefore blew the budget and both
        # pipelines were torn down before the greeting — the caller hears *nothing*
        # and the only clue is a dangling `greeting` task. This is a ceiling, not a
        # delay: a session that sets up quickly is unaffected. The ~13s itself is a
        # P5 problem.
        setup_timeout_secs=45.0,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )

    # Why the call ended, as far as we observed it. A one-element list rather than
    # a `nonlocal`, because `deliver` is defined before the handlers that would
    # need to see the assignment. Defaults to "disconnect": a caller who closes
    # the tab tells us nothing, and guessing a friendlier reason would make the
    # stored `end_reason` useless for telling a finished call from a dropped one.
    ended = ["disconnect"]

    async def deliver(event: dict[str, Any]) -> None:
        """The session's own channel to its own client — nobody else's."""
        await worker.rtvi.send_server_message(event)
        if event.get("type") == END_EVENT:
            ended[0] = str(event.get("reason") or "end_call")
            closer.arm()

    tool_ctx.deliver = deliver

    running = asyncio.Event()

    @worker.event_handler("on_pipeline_started")
    async def _on_started(_worker, _frame):
        running.set()

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        log.info("client connected")

        async def greet() -> None:
            # Processors drop frames that arrive before StartFrame, and with
            # SmallWebRTC the peer connection is often already up by the time
            # the pipeline starts — so wait, in a task. Awaiting `running` in
            # the handler itself would deadlock: pipeline start waits on
            # transport start, which waits on this handler.
            await running.wait()

            # Everything below is after `running`, RTVI message included. Sending
            # one before the pipeline has started does not merely get dropped like
            # a queued frame — it blocks, and the worker gives up with "timeout
            # setting the pipeline up" ~20s later, so the caller hears nothing at
            # all. `peek`, not `consume`: finding out whether a turn is available
            # must not spend one, and the greeting never reaches the LLM anyway.
            allowance = await quota.peek(resources, user)
            # One event at connect so a client can render the allowance without a
            # second request. It goes stale as the call proceeds — pushing an
            # update per turn belongs with the UI that would display it, in P4.
            await deliver({"type": QUOTA_EVENT,
                           "remaining": allowance.remaining,
                           "limit": allowance.limit,
                           "authenticated": allowance.authenticated})

            # A fixed greeting rather than an LLM turn: it is instant, always in
            # the right words, and the model cannot decide to open with
            # something else.
            #
            # Only the TTS frame — no LLMMessagesAppendFrame. The assistant
            # aggregator already records spoken text into the context, so
            # appending it here as well put the greeting in twice, which is how
            # the first draft of this ended up with a context whose last two
            # messages were identical.
            if not allowance.allowed:
                # Say so up front rather than greeting warmly and then refusing
                # the first question. Arming the closer here ends the call once
                # this sentence is out, so the caller is not left holding an open
                # line they cannot use.
                log.info(f"no turns left at connect (user={user.id}), closing")
                closer.arm()
                await worker.queue_frames([TTSSpeakFrame(
                    quota_exhausted_greeting(allowance.authenticated))])
                return
            await worker.queue_frames([TTSSpeakFrame(GREETING)])

        worker.create_task(greet(), name="greeting")

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        log.info("client disconnected")
        await worker.cancel()

    @worker.event_handler("on_idle_timeout")
    async def _on_idle(_worker):
        log.info(f"idle for {cfg.idle_timeout_secs}s, ending the call")
        await deliver({"type": END_EVENT, "reason": "inactivity"})
        await worker.stop_when_done()

    log.info(f"session start (transport={cfg.transport}, model={cfg.llm_model})")
    # handle_sigint=False: this runner lives inside uvicorn, one per call.
    # Letting it install process signal handlers would mean the newest caller
    # owns Ctrl-C for the whole server.
    runner = WorkerRunner(handle_sigint=False)
    try:
        await runner.run(worker)
    finally:
        log.info("session end")
        if recorder is not None:
            # In the `finally` so a call that ended badly is still recorded, and
            # wrapped because a failed write must not turn a completed call into an
            # exception propagating out of the request handler that started it.
            try:
                stored = await conv_service.append(
                    resources, conversation_id, recorder.turns)
                await conv_service.finish(
                    resources, conversation_id, reason=ended[0],
                    turn_count=recorder.turn_count, language=recorder.language)
                log.info(f"stored {stored} transcript messages "
                         f"({recorder.turn_count} turns, {ended[0]})")
            except Exception as exc:                            # noqa: BLE001
                log.error(f"could not store the transcript: "
                          f"{type(exc).__name__}: {exc}")
