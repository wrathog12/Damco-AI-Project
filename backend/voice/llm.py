"""
The `/chat` tool loop — text only.

Voice no longer comes through here. Pipecat's `GoogleLLMService` owns the spoken
conversation: it streams, it aggregates context, and it invokes the tool handlers
in `tools/registry.py` itself. What remains is the text endpoint, which the plan
keeps deliberately — it is the fastest way to exercise the five tools without
audio, and it was the gate that verified P1.

So `chat_streaming` is gone (Pipecat does it better, on the event loop) and
`chat` is async. v1 called the fully synchronous version directly on the event
loop, blocking the whole server for the length of three round-trips.

## Why this talks to google-genai and not an OpenAI-shaped client

Gemini has an OpenAI-compatible endpoint, and pointing `AsyncOpenAI` at it would
have been a two-line change that kept this file's original shape. It does not
work. Gemini 3 requires a `thought_signature` to be echoed back on every
`functionCall` part that it is being asked to continue from, and the compat layer
drops it — so round one returns a tool call and round two fails, every time:

    400 INVALID_ARGUMENT: Function call is missing a thought_signature in
    functionCall parts. This is required for tools to work correctly.

Since the tool loop *is* this endpoint, that is not a corner case. The fix is to
hold the model's own turn verbatim (`_append_model_turn`) rather than rebuild it
from the parts we care about, which is only expressible on the native SDK.

Two consequences worth knowing:

* **Signatures live for one request, not one conversation.** `history` crosses
  the wire as JSON and a signature is raw bytes, so it cannot survive the round
  trip to the client. That is fine: Gemini only demands the signature while the
  call is the turn it is continuing from. Once an assistant reply follows it, the
  call is ordinary history and unsigned is accepted — which is also why the
  seeded few-shot tool call in `prompts.py` does not need one.
* **The wire history stays OpenAI-shaped.** It is the endpoint's public contract
  and the frontend echoes it back untouched, so the provider swap is invisible
  from outside. `_to_contents` translates it on the way in.
"""
import json
from typing import Any

from google import genai
from google.genai import types
from loguru import logger

from config import settings
from tools.registry import GEMINI_TOOLS, ToolContext, dispatch
from voice.prompts import build_messages

MAX_TOOL_ROUNDS = 3

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """One HTTP client for the process.

    A connection pool is not session state — it holds no conversation and no
    caller's data — so this is not the kind of global cross-cutting rule 2 bans.
    Everything that *is* per-caller arrives as an argument.
    """
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


async def close_client() -> None:
    """Called from the FastAPI lifespan so the pool doesn't outlive the app."""
    global _client
    if _client is not None:
        await _client.aio.aclose()
        _client = None


def _to_contents(messages: list[dict]) -> tuple[str | None, list[types.Content]]:
    """OpenAI-shaped wire history → `(system_instruction, contents)`.

    Gemini takes the system prompt as a separate argument rather than a message,
    and calls the assistant `"model"`. A tool result is a `user` turn carrying a
    `functionResponse` — not a role of its own — so a reply to two parallel calls
    is two parts of one turn, which is why they are merged into the previous
    content when possible.
    """
    system: str | None = None
    contents: list[types.Content] = []

    for msg in messages:
        role = msg.get("role")

        if role == "system":
            # Several may accumulate (the voice path's LanguageTagger appends
            # one). Join rather than let the last win.
            text = (msg.get("content") or "").strip()
            system = f"{system}\n\n{text}" if system and text else (system or text)
            continue

        if role == "tool":
            raw = msg.get("content") or "{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            part = types.Part(function_response=types.FunctionResponse(
                name=msg.get("name") or _name_for_call_id(messages, msg),
                response=_as_response_object(payload),
            ))
            if contents and contents[-1].role == "user" and _is_response_turn(contents[-1]):
                contents[-1].parts.append(part)   # sibling of a parallel call
            else:
                contents.append(types.Content(role="user", parts=[part]))
            continue

        parts: list[types.Part] = []
        if msg.get("content"):
            parts.append(types.Part(text=msg["content"]))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            parts.append(types.Part(function_call=types.FunctionCall(
                name=fn.get("name", ""), args=args)))
        if parts:
            contents.append(types.Content(
                role="model" if role == "assistant" else "user", parts=parts))

    return system, contents


def _as_response_object(payload: Any) -> dict[str, Any]:
    """Gemini's `functionResponse.response` must be an object."""
    return payload if isinstance(payload, dict) else {"result": payload}


def _is_response_turn(content: types.Content) -> bool:
    return any(p.function_response for p in (content.parts or []))


def _name_for_call_id(messages: list[dict], tool_msg: dict) -> str:
    """Find the call a `tool` message answers, so it can be named.

    Gemini matches a response to its call by name; the OpenAI format matches by
    `tool_call_id`. Anything the client sends us is in the OpenAI shape, so the
    name has to be looked back up.
    """
    wanted = tool_msg.get("tool_call_id")
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            if call.get("id") == wanted:
                return call.get("function", {}).get("name", "")
    return ""


async def _complete(
    system: str | None,
    contents: list[types.Content],
    *,
    allow_tools: bool = True,
) -> types.GenerateContentResponse:
    """One Gemini call. `allow_tools=False` forces prose instead of another call."""
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.6,
        # See config: Gemini charges thinking against this budget, so too small a
        # cap returns an empty message — which on this path looks like "the model
        # had nothing to say".
        max_output_tokens=settings.llm_max_tokens,
        thinking_config=types.ThinkingConfig(
            thinking_level=settings.llm_thinking_level),
        # Already `Tool`-shaped — the adapter returns
        # `[{"function_declarations": [...]}]`, so wrapping it in another
        # `types.Tool` is a pydantic `extra_forbidden` error, not a no-op.
        #
        # The tools have to stay declared even when they are switched off below,
        # or Gemini rejects the history that mentions them.
        tools=GEMINI_TOOLS,
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
            mode="AUTO" if allow_tools else "NONE")),
    )
    return await client.aio.models.generate_content(
        model=settings.llm_model, contents=contents, config=config)


def _calls(response: types.GenerateContentResponse) -> list[types.FunctionCall]:
    cand = response.candidates[0] if response.candidates else None
    parts = (cand.content.parts if cand and cand.content else None) or []
    return [p.function_call for p in parts if p.function_call]


def _text(response: types.GenerateContentResponse) -> str:
    """The prose parts only — `response.text` warns when tool calls are present."""
    cand = response.candidates[0] if response.candidates else None
    parts = (cand.content.parts if cand and cand.content else None) or []
    return "".join(p.text for p in parts if p.text).strip()


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
    itself. The voice path's language tag now lives in `voice/pipeline.py`, where
    the STT result actually is.
    """
    conversation_history.append({"role": "user", "content": user_message})

    system, contents = _to_contents(build_messages(conversation_history))
    response = await _complete(system, contents)

    rounds = 0
    while _calls(response) and rounds < MAX_TOOL_ROUNDS:
        rounds += 1
        calls = _calls(response)

        # The model's own turn, appended object-for-object. Rebuilding it from
        # the call name and arguments — the obvious thing, and what the OpenAI
        # shape forces — discards the thought_signature Gemini requires back,
        # and the next call fails with a 400 that names no field we set.
        contents.append(response.candidates[0].content)
        conversation_history.append({
            "role": "assistant",
            "content": _text(response),
            "tool_calls": [
                {
                    "id": f"call_{rounds}_{i}",
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(dict(call.args or {}),
                                                ensure_ascii=False),
                    },
                }
                for i, call in enumerate(calls)
            ],
        })

        # All of a turn's responses belong to one `user` content, in the order
        # the calls were made.
        response_parts: list[types.Part] = []
        for i, call in enumerate(calls):
            args = dict(call.args or {})
            logger.info(f"[TOOL] {call.name}({args})")
            result = await dispatch(call.name, args, ctx)

            response_parts.append(types.Part(function_response=types.FunctionResponse(
                name=call.name, response=_as_response_object(result))))
            conversation_history.append({
                "role": "tool",
                "tool_call_id": f"call_{rounds}_{i}",
                # Kept for the name→call lookup on the next request, where the
                # OpenAI-shaped history has no other way to pair them up.
                "name": call.name,
                # ensure_ascii=False: scheme text is Devanagari/Bengali and
                # escaping it doubles the token count for no benefit.
                "content": json.dumps(result, ensure_ascii=False),
            })
        contents.append(types.Content(role="user", parts=response_parts))

        # On the last permitted round, take tools off the table. Otherwise the
        # model spends the round retrying a search and the loop exits holding a
        # tool call with no prose — which is how v1 returned an empty response
        # to three of the eight queries in the regression set.
        response = await _complete(system, contents,
                                   allow_tools=rounds < MAX_TOOL_ROUNDS)

    response_text = _text(response)
    conversation_history.append({"role": "assistant", "content": response_text})
    return response_text, conversation_history
