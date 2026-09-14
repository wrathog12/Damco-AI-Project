"""
Prompt engineering for Bhasha-Agent LLM.

Separated from llm.py for maintainability. Edit the system prompt and few-shot
examples here, never inline in the calling code.

The two callers consume this differently:

* **Voice (Pipecat).** `SYSTEM_PROMPT` goes to `GoogleLLMService.Settings(
  system_instruction=...)` — it lives outside the message list, so it survives
  every context update and every summarisation. The few-shots come from
  `seed_messages()` as the opening `LLMContext` messages.
* **Text (`/chat`).** `build_messages()` assembles system + few-shots + history
  on every turn, because there is no context aggregator on that path.
  `voice/llm.py::_to_contents` then splits the system message back out, since
  Gemini takes it as an argument rather than a message.

The tool call inside `FEW_SHOT_EXAMPLES` has no `thought_signature`, which Gemini
3 demands on a `functionCall` it is being asked to continue from. It is accepted
here because an assistant reply always follows it, which makes it ordinary
history rather than the turn in progress — so if you ever reorder these examples,
do not leave the tool call last.
"""
import copy

# ── System Prompt ───────────────────────────────────────
SYSTEM_PROMPT = """You are Bhasha-Agent, a warm multilingual VOICE assistant helping Indian citizens discover government welfare schemes.

## THIS IS SPOKEN, NOT WRITTEN
- 2-3 sentences MAX per reply. No lists, no bullets, no markdown — it is read aloud.
- Always end with one short question, in the user's own language.
- Name 1-2 schemes with a one-line summary each, then ask which one interests them. Reveal details progressively; never dump a whole scheme at once.
- Talk like a helpful friend, not a bureaucrat reading a document.

## LANGUAGE — the rule users complain about most
- Reply in the SAME language AND THE SAME SCRIPT as the user's latest message. This is MANDATORY.
- English in, English out. If they wrote or spoke plain English, answer in plain English — NOT Hindi, NOT Hinglish, and do not sprinkle in Hindi words. This is the #1 rule.
- Devanagari in, Devanagari out. If they wrote in Devanagari, reply in Devanagari — never transliterate Hindi into Latin letters.
- Hinglish (Hindi in Latin letters) in, Hinglish out. Bengali in, Bengali out. Marathi in, Marathi out.
- Match the USER, never the examples below. Those examples are Hinglish only because the user in them wrote Hinglish; an English question gets an English answer even when it is about the same scheme.
- Tool arguments are ALWAYS in English, whatever the conversation language. Everyday words are fine ("farming", "scholarship") — the server normalises them.

## TOOLS
1. Call search_schemes FIRST to discover anything. Never invent a scheme_id; only use ids a search returned.
2. Always give search_schemes a "query" saying what the user wants, in English ("scholarship for girl students", "loan to open a shop"). The other arguments are filters that narrow it down — a search with filters and no query is much weaker.
3. If a search returns 0 results, drop one filter and search again.
4. NEVER guess a "state" the user has not named — not from an example, not from a scheme you remember. If they have not told you their state, either ask them or omit the filter entirely. Naming the wrong state sends someone to a scheme they cannot apply for.
5. Call show_scheme_card when they say "dikhao", "show me", "apply karna hai", "detail de do".
6. Call end_call when they say goodbye or are done, then give a brief warm goodbye."""


# ── Few-Shot Examples ───────────────────────────────────
# Two examples, not the five-turn set v1 carried. They are prompt tokens on every
# single call and every call is now billed, so the budget argument still holds
# even without Groq's per-minute cliff (see services/schemes.py::_ALIASES).
#
# There is a second cost, and it is the one that bit: few-shots teach STYLE, not
# just shape. With only Hinglish examples the model generalised "Indian welfare
# scheme" to "answer in Hinglish" and replied to plain English questions in
# Hinglish. Every example added here is a language the model will imitate, so add
# them deliberately. What survived is the pair that teaches
# something the instructions above cannot state as compactly: the *shape* of a
# search call with a query plus filters, and that a missing state is a question
# rather than a guess. The dropped third example only re-demonstrated the first.

FEW_SHOT_EXAMPLES = [
    # ── Hinglish query → search with query + filters → short answer + question ──
    {
        "role": "user",
        "content": "Bihar mein kisan ke liye koi scheme hai?"
    },
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "ex1",
            "type": "function",
            "function": {
                "name": "search_schemes",
                "arguments": '{"query": "help for farmers, crop loss and farming support", "state": "Bihar", "occupation": "Farmer"}'
            }
        }]
    },
    {
        "role": "tool",
        "tool_call_id": "ex1",
        "content": '{"matches": 3, "schemes": [{"scheme_id": "bihar_fasal_sahayata", "scheme_name": "Bihar Rajya Fasal Sahayata Yojana", "state": "Bihar", "description": "Financial assistance to farmers for crop loss from natural calamities."}]}'
    },
    {
        "role": "assistant",
        "content": "Haan, Bihar mein kisaanon ke liye Fasal Sahayata Yojana hai — praakritik aapda se fasal ka nuksaan hone par madad milti hai. Kya aap iski eligibility jaanna chahenge?"
    },

    # ── Missing state → ask, don't guess. In English, deliberately: ──
    # with only Hinglish examples the model generalised "Indian welfare scheme"
    # to "answer in Hinglish" and replied to plain English questions in Hinglish,
    # which is the complaint the language rule above exists to prevent. Saying it
    # in the instructions was not enough; showing it once was.
    {
        "role": "user",
        "content": "I need a health insurance scheme"
    },
    {
        "role": "assistant",
        "content": "Health schemes differ by state — which state are you in?"
    },
]


GREETING = (
    "Hello! I am Bhasha Agent, your voice assistant for government welfare "
    "schemes. Which state are you from, and what kind of scheme are you looking "
    "for?"
)


# ── Quota ───────────────────────────────────────────────
# `voice/quota_gate.py` injects these two as one-turn `system` messages rather
# than adding them to SYSTEM_PROMPT, because each is true for exactly one turn.
# "You are out of turns" carried in the instruction would be spoken on a call
# where it is not true, and the model cannot be told to forget it.
#
# The instruction is in English while the *reply* must be in the caller's
# language: that split is the point. Pre-translating a refusal into ten languages
# would drift from the corpus and from each other, and the model is already
# holding the LanguageTagger's note about which language the caller is using —
# so the one place that knows the language is the one asked to write the sentence.

def quota_last_turn_notice(authenticated: bool) -> str:
    """Injected on the turn that is granted *last*, so the caller is warned while
    still being answered. Being cut off with no notice is what users experience as
    broken; the limit itself they accept."""
    ask = ("they can continue after a short wait" if authenticated else
           "signing in with their phone number will give them many more")
    return ("System notice: this is the user's last available turn for now. "
            "Answer their question normally, then add ONE short sentence, in the "
            f"user's own language, saying this was their last turn and that {ask}. "
            "Do not apologise and do not explain how the limit works.")


def quota_exhausted_instruction(authenticated: bool) -> str:
    """Injected in place of an answer once the allowance is gone.

    The gate also strips the tools off the context before this runs, so "do not
    look anything up" is enforced rather than merely requested — the model has no
    tool to call. Saying it anyway keeps the model from promising to.
    """
    offer = ("they can continue after a short wait" if authenticated else
             "if they sign in with their phone number they will get many more")
    return ("System notice: the user has no turns left, so their last question "
            "cannot be answered. Do not answer it, do not look anything up and "
            "do not promise to. In the user's own language, say only this in two "
            f"short sentences: their turns are finished for now, and {offer}. "
            "Then give a brief warm goodbye.")


def quota_exhausted_greeting(authenticated: bool) -> str:
    """Spoken instead of `GREETING` when the allowance is already gone at connect.

    English, unlike the two above, and not a model call: nothing has been said
    yet, so there is no language to match and no context to reply into. Telling
    someone up front beats greeting them warmly and then refusing their question.
    """
    if authenticated:
        return ("Hello! You have used all your turns for now. They will refresh "
                "shortly — please call back a little later. Thank you!")
    return ("Hello! You have used your free turns for now. Please sign in with "
            "your phone number to keep talking and get many more. Thank you!")


def seed_messages() -> list[dict]:
    """The opening `LLMContext` messages for a voice session.

    Few-shots only — no system message: that is `system_instruction` on the LLM
    service, which is a better home for it because context summarisation can
    rewrite messages but cannot touch the instruction.

    A fresh list every call. Handing the same list to two sessions would let
    each session's aggregator append the other's turns to it, which is exactly
    the process-global `conversation_history` bug v2 exists to remove.
    """
    return copy.deepcopy(FEW_SHOT_EXAMPLES)


def build_messages(conversation_history: list[dict]) -> list[dict]:
    """
    Build the final message list for the LLM:
    [system] + [few-shot examples] + [actual conversation]

    The few-shot examples are stripped after the first real tool call
    to avoid bloating context in long conversations.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Only include few-shot examples if conversation is short
    if len(conversation_history) < 6:
        messages.extend(FEW_SHOT_EXAMPLES)

    # STRICT CONTEXT TRUNCATION: keep only the last 10 messages.
    #
    # Inherited from v1, where it was a workaround for Groq's free tier throttling
    # long-context requests (1s -> 35s+). That specific reason is gone, but the
    # truncation stays on this path: /chat has no context aggregator, so without
    # it a long session grows the prompt without bound. The voice path replaces it
    # with real auto context summarisation instead of dropping turns.
    recent_history = conversation_history[-10:] if len(conversation_history) > 10 else conversation_history
    
    # Ensure we don't accidentally start with a 'tool' response if the previous
    # 'assistant' tool call got truncated.
    while recent_history and recent_history[0]["role"] == "tool":
        recent_history = recent_history[1:]

    messages.extend(recent_history)
    return messages
