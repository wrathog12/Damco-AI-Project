"""Shared Pydantic models used across backend modules."""
from pydantic import BaseModel
from typing import Optional


class SchemeSearchResult(BaseModel):
    """Compact scheme info returned by search_schemes tool."""
    scheme_id: str
    scheme_name: str
    category: str
    state: str
    description: str  # truncated to ~120 chars


class CardPayload(BaseModel):
    """WebSocket payload sent to frontend to render a scheme card."""
    type: str = "show_card"
    scheme: dict
    language: str = "en"


class CardDismissPayload(BaseModel):
    """WebSocket payload sent from frontend when card is closed."""
    type: str = "dismiss_card"


class EligibilityResult(BaseModel):
    """Result of check_eligibility tool."""
    eligible: bool
    scheme_id: str
    scheme_name: str
    reasons: list[str]


class ToolResult(BaseModel):
    """Wrapper for any tool execution result."""
    tool_name: str
    success: bool
    data: dict | list | str
