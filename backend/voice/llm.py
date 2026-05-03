"""
LLM wrapper — Groq Llama 3.3 70B with streaming + tool-calling.

Two modes:
  - chat()           → non-streaming, returns full response (for /chat endpoint)
  - chat_streaming()  → streaming, yields sentences as they arrive (for voice pipeline)
"""
import json
import re
from groq import Groq
from config import settings
from tools.registry import TOOL_SCHEMAS, dispatch
from voice.prompts import build_messages

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=settings.groq_api_key)
    return _client


# ── Sentence boundary detection ────────────────────────
# Splits on . ? ! but avoids splitting on abbreviations like "Dr." or "Rs."
_SENTENCE_END = re.compile(r'(?<=[.?!])\s+')


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences at natural boundaries."""
    parts = _SENTENCE_END.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


# ── Non-streaming chat (for /chat text endpoint) ───────
def chat(
    user_message: str,
    conversation_history: list[dict],
    detected_language: str = "en",
) -> tuple[str, list[dict]]:
    """
    Send a message to Llama 3.3 with tool-calling support.
    Non-streaming — returns full response. Used by /chat endpoint.

    Returns:
        (response_text, updated_history)
    """
    client = _get_client()

    conversation_history.append({
        "role": "user",
        "content": user_message,
    })

    messages = build_messages(conversation_history)

    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        tools=TOOL_SCHEMAS,
        tool_choice="auto",
        max_tokens=256,
        temperature=0.6,
    )

    msg = response.choices[0].message

    # Handle tool calls
    max_rounds = 3
    rounds = 0

    while msg.tool_calls and rounds < max_rounds:
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
                args = {}

            print(f"  [TOOL] {tc.function.name}({args})")
            result = dispatch(tc.function.name, args)

            conversation_history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        messages = build_messages(conversation_history)

        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            max_tokens=256,
            temperature=0.6,
        )

        msg = response.choices[0].message

    response_text = msg.content or ""

    conversation_history.append({
        "role": "assistant",
        "content": response_text,
    })

    return response_text, conversation_history


# ── Streaming chat (for voice pipeline) ────────────────
def chat_streaming(
    user_message: str,
    conversation_history: list[dict],
    detected_language: str = "en",
):
    """
    Streaming LLM with tool-calling. Yields sentences as they complete.

    Tool calls are handled internally (non-streaming, since they're fast).
    Only the FINAL text response is streamed sentence-by-sentence.

    Yields:
        (sentence: str, is_final: bool)

    After all sentences are yielded, conversation_history is updated in-place.
    """
    client = _get_client()

    # Map Deepgram language codes to readable names
    lang_map = {"en": "English", "hi": "Hindi", "bn": "Bengali", "mr": "Marathi"}
    lang_label = lang_map.get(detected_language, detected_language)

    # Tag the user message with detected language so LLM knows what to respond in
    tagged_message = f"[User is speaking {lang_label}] {user_message}"

    conversation_history.append({
        "role": "user",
        "content": tagged_message,
    })

    messages = build_messages(conversation_history)

    # ── First: handle tool calls (non-streaming) ────────
    # Tool call responses are short JSON, streaming doesn't help.
    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        tools=TOOL_SCHEMAS,
        tool_choice="auto",
        max_tokens=256,
        temperature=0.6,
    )

    msg = response.choices[0].message
    max_rounds = 2
    rounds = 0

    while msg.tool_calls and rounds < max_rounds:
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
                args = {}

            print(f"  [TOOL] {tc.function.name}({args})")
            result = dispatch(tc.function.name, args)

            conversation_history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        messages = build_messages(conversation_history)

        # Check if more tool calls are needed
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            max_tokens=256,
            temperature=0.6,
        )
        msg = response.choices[0].message

    # ── If the non-streaming response already has content, check if
    #    it came from a tool-call round. If so, we already have the
    #    full response — stream it sentence-by-sentence without
    #    another API call.
    if msg.content and not msg.tool_calls:
        # We got the final response from the tool-call round
        full_text = msg.content
        sentences = _split_sentences(full_text)

        if not sentences:
            sentences = [full_text]

        for i, sentence in enumerate(sentences):
            is_final = (i == len(sentences) - 1)
            yield (sentence, is_final)

        conversation_history.append({
            "role": "assistant",
            "content": full_text,
        })
        return

    # ── Stream the final response sentence-by-sentence ──
    messages = build_messages(conversation_history)

    stream = client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        max_tokens=256,
        temperature=0.6,
        stream=True,
    )

    buffer = ""
    full_response = ""

    for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            buffer += delta.content
            full_response += delta.content

            # Check for sentence boundaries
            sentences = _split_sentences(buffer)

            if len(sentences) > 1:
                # Yield all complete sentences, keep the last as buffer
                for sentence in sentences[:-1]:
                    yield (sentence, False)
                buffer = sentences[-1]

    # Yield remaining buffer as the final sentence
    if buffer.strip():
        yield (buffer.strip(), True)

    # Update history with full response
    conversation_history.append({
        "role": "assistant",
        "content": full_response,
    })
