"""
Tool registry — defines tool schemas for LLM and dispatches calls.
"""
import json
from tools.search import search_schemes
from tools.details import get_scheme_details
from tools.card import show_scheme_card_sync
from tools.eligibility import check_eligibility
from tools.end_call import end_call_sync


# ── Tool Schemas (OpenAI format — used by Groq) ────────
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_schemes",
            "description": (
                "Search the knowledge base for government welfare schemes "
                "matching the given criteria. Use this when the user asks about "
                "available schemes, wants to discover schemes, or describes their "
                "demographics/needs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "description": "State name, e.g. 'Bihar', 'Maharashtra', 'Central Government'"
                    },
                    "category": {
                        "type": "string",
                        "description": "Scheme category, e.g. 'Education & Learning', 'Health & Wellness', 'Agriculture & Rural Development', 'Social Welfare & Empowerment', 'Business & Entrepreneurship', 'Housing & Shelter', 'Skills & Employment'"
                    },
                    "age": {
                        "type": "integer",
                        "description": "User's age in years"
                    },
                    "gender": {
                        "type": "string",
                        "enum": ["Male", "Female", "All"],
                        "description": "User's gender"
                    },
                    "occupation": {
                        "type": "string",
                        "description": "User's occupation, e.g. 'Farmer', 'Student', 'Worker'"
                    },
                    "income": {
                        "type": "integer",
                        "description": "User's annual income in INR"
                    },
                    "caste": {
                        "type": "string",
                        "description": "User's caste category: SC, ST, OBC, General"
                    },
                    "disability": {
                        "type": "boolean",
                        "description": "Whether the user has a disability"
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scheme_details",
            "description": (
                "Get full details about a specific government scheme. "
                "Use this when the user asks about a specific scheme's details, "
                "benefits, eligibility, documents, or application process."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scheme_id": {
                        "type": "string",
                        "description": "The scheme ID returned by search_schemes"
                    },
                },
                "required": ["scheme_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_scheme_card",
            "description": (
                "Show a visual scheme card on the user's screen with all details "
                "and an apply button. Call this when: (1) the conversation about a "
                "scheme is concluding, (2) the user wants to see details visually, "
                "or (3) the user wants to apply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scheme_id": {
                        "type": "string",
                        "description": "The scheme ID to display"
                    },
                    "language": {
                        "type": "string",
                        "enum": ["en", "hi", "bn", "mr"],
                        "description": "Language for the card display. Default 'en'."
                    },
                },
                "required": ["scheme_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_eligibility",
            "description": (
                "Check if a user is eligible for a specific scheme based on their "
                "demographics. Use when the user asks 'Am I eligible?' or shares "
                "their age/income/state/gender."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scheme_id": {
                        "type": "string",
                        "description": "The scheme ID to check eligibility against"
                    },
                    "user_age": {"type": "integer", "description": "User's age"},
                    "user_gender": {"type": "string", "description": "User's gender"},
                    "user_state": {"type": "string", "description": "User's state of residence"},
                    "user_income": {"type": "integer", "description": "User's annual income in INR"},
                    "user_occupation": {"type": "string", "description": "User's occupation"},
                },
                "required": ["scheme_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": (
                "End the voice call. Call this when: (1) the user says goodbye, "
                "thanks you, or indicates they are done (e.g. 'nhi thank you', "
                "'dhanyawad', 'bas itna hi', 'bye', 'theek hai thank you', "
                "'nahi chahiye aur kuch'), or (2) the user has no further questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the call is ending, e.g. 'user_goodbye', 'user_thanked', 'no_more_questions'"
                    },
                },
                "required": ["reason"],
            },
        },
    },
]


# ── Dispatcher ──────────────────────────────────────────
_TOOL_MAP = {
    "search_schemes": search_schemes,
    "get_scheme_details": get_scheme_details,
    "show_scheme_card": show_scheme_card_sync,
    "check_eligibility": check_eligibility,
    "end_call": end_call_sync,
}


def dispatch(tool_name: str, arguments: dict) -> str:
    """
    Execute a tool by name with the given arguments.
    Returns the JSON string result.
    """
    fn = _TOOL_MAP.get(tool_name)
    if not fn:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    try:
        # Strip null values so function uses its own defaults
        cleaned_args = {k: v for k, v in arguments.items() if v is not None}
        return fn(**cleaned_args)
    except Exception as e:
        return json.dumps({"error": f"Tool '{tool_name}' failed: {str(e)}"})
