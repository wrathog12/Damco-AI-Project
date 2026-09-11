"""
The `/chat` tool loop — text only.

Voice no longer comes through here. Pipecat's `GroqLLMService` owns the spoken
conversation: it streams, it aggregates context, and it invokes the tool handlers
in `tools/registry.py` itself. What remains is the text endpoint, which the plan
keeps deliberately — it is the fastest way to exercise the five tools without
audio, and it was the gate that verified P1.

So `chat_streaming` is gone (Pipecat does it better, on the event loop) and
`chat` is async. v1 called the fully synchronous version directly on the event
loop, blocking the whole server for the length of three Groq round-trips.
"""
import json

from groq import AsyncGroq
from loguru import logger

from config import settings
from tools.registry import OPENAI_TOOL_SCHEMAS, ToolContext, dispatch
from voice.prompts import build_messages

MAX_TOOL_ROUNDS = 3

_client: AsyncGroq | None = None


def _get_client() -> AsyncGroq:
    """One HTTP client for the process.

    A connection pool is not session state — it holds no conversation and no
    caller's data — so this is not the kind of global cross-cutting rule 2 bans.
    Everything that *is* per-caller arrives as an argument.
    """
    global _client
    if _client is None:
        _client = AsyncGroq(api_key=settings.groq_api_key)
    return _client


async def close_client() -> None:
    """Called from the FastAPI lifespan so the pool doesn't outlive the app."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def _complete(messages: list[dict], *, allow_tools: bool = True):
    """One Groq call. `allow_tools=False` forces prose instead of another call."""
    client = _get_client()
    response = await client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        tools=OPENAI_TOOL_SCHEMAS,
        tool_choice="auto" if allow_tools else "none",
        # See config: too small a budget on a reasoning model returns an empty
        # message, which on this path looked like "the model had nothing to say".
        max_tokens=settings.llm_max_tokens,
        reasoning_effort=settings.llm_reasoning_effort,
        temperature=0.6,
    )
    return response.choices[0].message


async def chat(
    ctx: ToolContext,
    user_message: str,
    conversation_history: list[dict],
    detected_language: str = "en",
) -> tuple[str, list[dict]]:
    """One text turn, tools included. Returns `(reply, history)`.

    `ctx` carries the database handles the tools read through and the `Deliver`
    that collects UI events — `/chat` passes an `EventCollector`, so a
    `show_scheme_card` call comes back in the HTTP response instead of being
    dropped the way v1's mismatched `show_card` broadcast was.

    `detected_language` is accepted and unused on this path: the text caller
    states its language in the request and the model reads it from the message
    itself. The voice path's `[User is speaking X]` tag now lives in
    `voice/pipeline.py`, where the STT result actually is.
    """
    conversation_history.append({"role": "user", "content": user_message})

    msg = await _complete(build_messages(conversation_history))

    rounds = 0
    while msg.tool_calls and rounds < MAX_TOOL_ROUNDS:
        rounds += 1

        conversation_history.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                # Hallucinated tool syntax. An empty dict lets the tool's own
                # defaults apply and the model see a real result instead of a
                # crash.
                args = {}

            logger.info(f"[TOOL] {tc.function.name}({args})")
            result = await dispatch(tc.function.name, args, ctx)

            conversation_history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                # ensure_ascii=False: scheme text is Devanagari/Bengali and
                # escaping it doubles the token count for no benefit.
                "content": json.dumps(result, ensure_ascii=False),
            })

        # On the last permitted round, take tools off the table. Otherwise the
        # model spends the round retrying a search and the loop exits holding a
        # tool call with no prose — which is how v1 returned an empty response
        # to three of the eight queries in the regression set.
        msg = await _complete(build_messages(conversation_history),
                              allow_tools=rounds < MAX_TOOL_ROUNDS)

    response_text = msg.content or ""
    conversation_history.append({"role": "assistant", "content": response_text})
    return response_text, conversation_history
