"""
The turn meter on the voice path.

`/chat` can refuse a turn by returning different JSON. A live call cannot: the
caller is mid-sentence, holding a phone, and the only way to tell them anything
is to say it out loud. So enforcement here is a processor that sits between the
user aggregator and the LLM, on the one frame that carries a turn into inference.

## Why here and not in the transport or a tool

`LLMContextFrame` is what makes the LLM generate. Every path that produces a
turn — a completed utterance, a re-run after a tool call, an appended message
with `run_llm=True` — funnels into it, so gating it gates all of them, and
nothing new has to be remembered when another path is added. Counting
transcriptions instead would over-count (one utterance can arrive as several
finals) and counting inside a tool handler would under-count (a turn that calls
no tool is still a turn).

## What denial looks like

Not a dropped connection, and not silence. The frame is still forwarded, but
first the context has its **tools removed** and a one-turn instruction appended
telling the model to say the allowance is gone. So the refusal:

* comes out in the caller's own language, because the model is the only part of
  the system that knows which language that is (see `LanguageTagger`);
* cannot answer the question it was just refused — with no tools it has nothing
  to search with, which makes that structural rather than a request the model
  might reinterpret;
* finishes its sentence before the call ends, because it arms the same
  `CallCloser` that `end_call` uses rather than adding a second teardown path.

Mutating the live context is safe *only* because this is the last turn of the
call. Nothing else in here writes to it.

## Speculative inference is never counted

Pipecat pushes `LLMContextFrame(speculation=True)` from a provisional context to
get a head start on a turn that may not have finished. Its answer may never reach
the caller, so charging for it would bill people for words they never heard.
Those frames pass through untouched — and once the allowance is known to be gone
they are dropped instead, since there is no turn left for the guess to win.
"""
from collections.abc import Callable

from loguru import logger

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from models.identity import User
from services import quota
from services.resources import Resources
from voice import prompts


class QuotaGate(FrameProcessor):
    """Spends one turn per real inference, and refuses in words when there is none.

    Placed between `aggregators.user()` and the LLM service. `on_exhausted` is
    `CallCloser.arm` — passed in rather than reached for, so this processor knows
    nothing about how the pipeline ends.
    """

    def __init__(
        self,
        *,
        resources: Resources,
        user: User,
        on_exhausted: Callable[[], None],
    ) -> None:
        super().__init__()
        self._resources = resources
        self._user = user
        self._on_exhausted = on_exhausted
        # Set once the allowance is known to be spent — by a denial, or by a grant
        # that was the last one. Only used to stop paying for speculation.
        self._out_of_turns = False
        # True from the moment a refusal is on its way to the TTS. Everything the
        # caller says over the top of it is dropped: they have no turns, the call
        # is closing, and a second refusal in the middle of the first one is worse
        # than silence.
        self._refused = False
        # The last-turn warning, held by identity so it can be removed again.
        self._notice: LLMContextMessage | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        if frame.speculation:
            if not self._out_of_turns:
                await self.push_frame(frame, direction)
            return

        if self._refused:
            return

        self._clear_notice(frame.context)
        verdict = await quota.consume(self._resources, self._user)

        if not verdict.allowed:
            self._refused = True
            self._out_of_turns = True
            logger.info(f"[quota] refusing turn, user={self._user.id} "
                        f"used={verdict.turns_used}/{verdict.limit}")
            # No tools, then the instruction. Order matters only for reading.
            frame.context.set_tools()
            frame.context.add_message({
                "role": "system",
                "content": prompts.quota_exhausted_instruction(
                    verdict.authenticated),
            })
            # Arm before forwarding: the closer fires on the *next*
            # BotStoppedSpeakingFrame, which is the end of this refusal. The bot
            # is not speaking right now — the caller just finished their turn —
            # so there is no earlier frame for it to catch.
            self._on_exhausted()
            await self.push_frame(frame, direction)
            return

        if verdict.is_last_turn:
            self._out_of_turns = True
            self._notice = {
                "role": "system",
                "content": prompts.quota_last_turn_notice(verdict.authenticated),
            }
            frame.context.add_message(self._notice)

        await self.push_frame(frame, direction)

    def _clear_notice(self, context: LLMContext) -> None:
        """Drop the previous turn's last-turn warning.

        It has to go, or the model repeats it: a `system` message stays in the
        context forever and the warning is only true of the turn it was added to.
        The window can also roll over mid-call, at which point "this is your last
        turn" is simply false. Removed at the start of the next turn rather than
        after the answer, because that is the first moment the model is certainly
        finished reading it.
        """
        if self._notice is None:
            return
        context.set_messages([m for m in context.messages if m is not self._notice])
        self._notice = None
