"""
The five tools: their schemas, and the one place they are dispatched from.

`TOOL_SCHEMAS` is a list of Pipecat `FunctionSchema`s carrying their own handlers,
so `LLMContext(tools=TOOL_SCHEMAS)` is all a session needs — the LLM service
registers each handler itself. The same list feeds `/chat`, which runs its own
small tool loop through `dispatch`.

Two things this module exists to prevent:

* **Two tool tables.** The voice path and the text path must advertise and
  execute exactly the same five tools, or `/chat` stops being a smoke test for
  the thing that ships. `_CORES` and `TOOL_SCHEMAS` are checked against each
  other at import.
* **Globals.** Every handler gets its database handles and its UI channel from
  the `ToolContext` in `app_resources` — per session, passed by reference.
  v1 read both from module globals shared by the whole process.

The five names are the stable contract (cross-cutting rule 1): P1 replaced their
bodies, P2 replaces the runtime around them, and neither is allowed to rename
them.
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.services.gemini_adapter import GeminiLLMAdapter
from pipecat.services.llm_service import FunctionCallParams

from models.identity import User
from services import conversations as conv_service
from services import eligibility as eligibility_service
from services import profiles as profile_service
from services.resources import Resources
from tools.card import show_scheme_card
from tools.details import get_scheme_details
from tools.eligibility import check_eligibility
from tools.end_call import end_call
from tools.events import Deliver
from tools.search import search_schemes


@dataclass
class ToolContext:
    """What a tool handler is allowed to reach — one per voice session.

    Handed to `PipelineWorker(app_resources=...)`, which is how it arrives as
    `FunctionCallParams.app_resources`. `deliver` is None on paths with no live
    client (a script, a test); the two tools that use it degrade to returning
    their result and skipping the UI event, which is the correct behaviour for a
    caller that has no screen.

    `user` is who the session belongs to, and it is what lets `check_eligibility`
    be called with a scheme id alone. It is optional for the same reason `deliver`
    is: a script or a `/health`-style probe has no caller, and a tool that raised
    without one would make the whole tool table untestable. The tools degrade to
    exactly the P2 behaviour — no memory, everything asked for explicitly.

    `conversation_id` travels with it so an interaction can be attributed to the
    call it happened on without the tool having to reach into the pipeline.
    """
    resources: Resources
    deliver: Deliver | None = None
    user: User | None = None
    conversation_id: str | None = None


Core = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


# ── the cores: arguments in, result dict out ────────────────────────────
async def _search(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await search_schemes(ctx.resources, **args)


async def _details(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await get_scheme_details(ctx.resources, **args)


async def _card(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    result = await show_scheme_card(ctx.resources, deliver=ctx.deliver, **args)
    # Recorded after the fact and never allowed to affect it: a card that reached
    # the caller's screen is the strongest signal of interest this system gets, and
    # it is also the cheapest memory to keep (see models/profile.py). `error` in
    # the result means no card was shown, so there is nothing to remember.
    if ctx.user is not None and "error" not in result and args.get("scheme_id"):
        await conv_service.record_interaction(
            ctx.resources, ctx.user, scheme_id=args["scheme_id"],
            kind="card_shown", conversation_id=ctx.conversation_id)
    return result


async def _eligibility(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Fills the gaps from the profile, then records the verdict.

    This is where "call check_eligibility with no arguments" actually becomes
    true. The loading is here rather than in `tools/eligibility.py` because that
    module has no business knowing there is such a thing as a logged-in user —
    it evaluates rules against facts, and the registry is the layer that already
    knows whose session this is.
    """
    profile = None
    if ctx.user is not None:
        profile = await profile_service.load(ctx.resources, ctx.user)

    result = await check_eligibility(
        ctx.resources,
        profile_facts=profile_service.eligibility_args(profile),
        **args)

    if ctx.user is not None and "error" not in result:
        await conv_service.record_interaction(
            ctx.resources, ctx.user, scheme_id=result["scheme_id"],
            kind="eligibility_checked", conversation_id=ctx.conversation_id)
        # Only cache a verdict computed from the *stored* facts alone. One that
        # used a value the model supplied this turn is not reproducible from the
        # profile, so filing it under `profile_version` would make the cache claim
        # something the profile does not say.
        if profile is not None and not any(k.startswith("user_") for k in args):
            await eligibility_service.remember(
                ctx.resources, ctx.user, result,
                profile_version=profile.version)
    return result


async def _end_call(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await end_call(deliver=ctx.deliver, **args)


_CORES: dict[str, Core] = {
    "search_schemes": _search,
    "get_scheme_details": _details,
    "show_scheme_card": _card,
    "check_eligibility": _eligibility,
    "end_call": _end_call,
}


# ── dispatch ────────────────────────────────────────────────────────────
async def dispatch(tool_name: str, arguments: dict[str, Any],
                   ctx: ToolContext) -> dict[str, Any]:
    """Run one tool. Never raises — a failure comes back as `{"error": ...}`.

    The LLM can recover from an error it can read; it cannot recover from an
    exception that kills the turn. `None` arguments are stripped so the Python
    defaults apply, because models routinely send `"state": null` for a filter
    they mean to omit.
    """
    core = _CORES.get(tool_name)
    if core is None:
        return {"error": f"Unknown tool: {tool_name}"}

    cleaned = {k: v for k, v in (arguments or {}).items() if v is not None}
    try:
        return await core(ctx, cleaned)
    except TypeError as exc:
        # An argument the schema does not declare, or a missing required one.
        logger.warning(f"tool {tool_name} called with {cleaned}: {exc}")
        return {"error": f"Tool '{tool_name}' called incorrectly: {exc}"}
    except Exception as exc:                                  # noqa: BLE001
        logger.exception(f"tool {tool_name} failed")
        return {"error": f"Tool '{tool_name}' failed: {exc}"}


def _make_handler(tool_name: str):
    """The Pipecat-side adapter for one tool."""
    async def handler(params: FunctionCallParams) -> None:
        ctx = params.app_resources
        if not isinstance(ctx, ToolContext):
            # Worth failing loudly: without a context every tool would report
            # "not found" and the agent would confidently tell callers there are
            # no schemes for them.
            await params.result_callback(
                {"error": "Tool context unavailable — the session was built "
                          "without app_resources."})
            return
        result = await dispatch(tool_name, dict(params.arguments), ctx)
        await params.result_callback(result)

    handler.__name__ = f"{tool_name}_handler"
    return handler


# ── schemas ─────────────────────────────────────────────────────────────
_CATEGORIES = [
    "Education & Learning",
    "Social welfare & Empowerment",
    "Agriculture,Rural & Environment",
    "Business & Entrepreneurship",
    "Banking,Financial Services and Insurance",
    "Health & Wellness",
    "Sports & Culture",
    "Skills & Employment",
    "Housing & Shelter",
    "Women and Child",
    "Travel & Tourism",
    "Science, IT & Communications",
    "Transport & Infrastructure",
    "Utility & Sanitation",
    "Public Safety,Law & Justice",
]

TOOL_SCHEMAS: list[FunctionSchema] = [
    FunctionSchema(
        name="search_schemes",
        description=(
            "Find welfare schemes. Call this first for any question about what "
            "is available. 'query' is the subject of the search; the rest are "
            "filters that narrow it."
        ),
        properties={
            # Semantic half of the hybrid index. Without it the search is
            # filter-only, which cannot answer "money for my daughter's
            # school fees" — see tools/search.py.
            "query": {
                "type": "string",
                "description": "What the user wants, in English, e.g. 'scholarship for girl students', 'loan to start a small shop'",
            },
            "state": {
                "type": "string",
                "description": "State name, or 'Central Government' for national schemes",
            },
            # An enum, not a free-string hint: these are myScheme's own labels
            # and the filter is an exact keyword match, so an invented near-miss
            # ("Social Welfare & Empowerment") would return zero rows. Everyday
            # words are also accepted — `services.schemes._ALIASES` maps them —
            # which is why the system prompt no longer carries this table.
            "category": {
                "type": "string",
                "enum": _CATEGORIES,
                "description": "Omit rather than guess — a wrong category filters everything out.",
            },
            "age": {"type": "integer", "description": "Age in years"},
            "gender": {
                "type": "string",
                "enum": ["Male", "Female", "All"],
            },
            "occupation": {
                "type": "string",
                "description": "e.g. 'Farmer', 'Student', 'Worker'",
            },
            "income": {"type": "integer", "description": "Annual income in INR"},
            "caste": {
                "type": "string",
                "description": "SC, ST, OBC or General",
            },
            "disability": {"type": "boolean"},
        },
        required=[],
        handler=_make_handler("search_schemes"),
    ),
    FunctionSchema(
        name="get_scheme_details",
        description=(
            "Full details of one scheme — benefits, eligibility, documents, "
            "how to apply."
        ),
        properties={
            "scheme_id": {
                "type": "string",
                "description": "An id returned by search_schemes",
            },
        },
        required=["scheme_id"],
        handler=_make_handler("get_scheme_details"),
    ),
    FunctionSchema(
        name="show_scheme_card",
        description=(
            "Put a scheme card with an apply button on the user's screen. Call "
            "it when they want to see or apply for a scheme."
        ),
        properties={
            "scheme_id": {"type": "string"},
            "language": {
                "type": "string",
                "enum": ["en", "hi", "bn", "mr"],
                "description": "Card language, default 'en'",
            },
        },
        required=["scheme_id"],
        handler=_make_handler("show_scheme_card"),
    ),
    FunctionSchema(
        name="check_eligibility",
        description=(
            "Check whether the user qualifies for one scheme. Use when they ask "
            "'am I eligible?' or give their age/income/state/gender. Details the "
            "user has given on an earlier call are filled in automatically, so "
            "scheme_id alone is enough — pass a value only when they state it in "
            "this conversation, and it will override what is remembered."
        ),
        properties={
            "scheme_id": {"type": "string"},
            "user_age": {"type": "integer"},
            "user_gender": {"type": "string"},
            "user_state": {"type": "string"},
            "user_income": {"type": "integer", "description": "Annual, in INR"},
            "user_occupation": {"type": "string"},
            # Additive — the five names are the stable contract, the argument
            # lists are allowed to grow. These two exist because the profile now
            # stores them and `evaluate_eligibility` now enforces them: a caste
            # the model heard this turn must be able to reach the rules, or the
            # only way to correct a stale one would be to edit the profile.
            "user_caste": {
                "type": "string",
                "description": "SC, ST, OBC, General or EWS",
            },
            "user_disability": {"type": "boolean"},
        },
        required=["scheme_id"],
        handler=_make_handler("check_eligibility"),
    ),
    FunctionSchema(
        name="end_call",
        description=(
            "End the voice call. Call this when: (1) the user says goodbye, "
            "thanks you, or indicates they are done (e.g. 'nhi thank you', "
            "'dhanyawad', 'bas itna hi', 'bye', 'theek hai thank you', "
            "'nahi chahiye aur kuch'), or (2) the user has no further questions."
        ),
        properties={
            "reason": {
                "type": "string",
                "description": "Why the call is ending, e.g. 'user_goodbye', 'user_thanked', 'no_more_questions'",
            },
        },
        # v1 marked this required while the function defaulted it, so a model
        # that omitted it produced a schema violation for no reason.
        required=[],
        handler=_make_handler("end_call"),
    ),
]


TOOL_NAMES = tuple(s.name for s in TOOL_SCHEMAS)

# The same five schemas as Gemini `functionDeclarations`. `/chat` calls the
# google-genai SDK directly rather than through a Pipecat pipeline, so it needs
# the provider format — but *derived* from `TOOL_SCHEMAS`, never hand-maintained
# alongside it, which is the whole point of this module.
#
# Converted with Pipecat's own adapter rather than by hand: it is what the voice
# path uses, so the two paths cannot advertise subtly different tools. It also
# does the JSON-Schema-to-Gemini adaptations that are easy to miss — Gemini
# rejects several keywords the OpenAI format allows.
GEMINI_TOOLS: list[dict[str, Any]] = GeminiLLMAdapter().to_provider_tools_format(
    ToolsSchema(standard_tools=list(TOOL_SCHEMAS))
)

# The advertised tools and the executable ones drifting apart is a silent
# failure: the LLM calls something that returns "Unknown tool", or a tool exists
# that nothing will ever call.
assert set(TOOL_NAMES) == set(_CORES), (
    f"schema/handler mismatch: {set(TOOL_NAMES) ^ set(_CORES)}")
