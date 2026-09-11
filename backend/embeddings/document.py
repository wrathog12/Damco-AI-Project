"""
The text that actually gets embedded.

This file matters more to retrieval quality than the choice of model does. Two
rules drive it:

1. **Embed what people ask about, not the whole record.** Dumping every column
   into one vector dilutes it — a 4,000-character legal description drowns out
   the scheme's name and purpose, which is what a caller actually says. So the
   document is a synthesised summary with the identifying fields first.

2. **Front-load the name.** Callers refer to schemes by name ("Kanyashree",
   "PM Vishwakarma"). Embedding models weight early tokens more heavily, and the
   sparse/BM25 half of the hybrid index keys on exact tokens, so the name and its
   short title lead.

Deliberately excluded: `application_process` (procedural, never what a caller
asks to *find*), `documents_required` (same), `references`, URLs and helplines
(no semantic content, pure noise in a vector).
"""
from typing import Any

# Roughly the point where extra text stops helping recall and starts diluting the
# vector. gemini-embedding-2 accepts far more than this; the limit is editorial.
MAX_DOC_CHARS = 4000


def _clip(text: str, budget: int) -> str:
    """Trim to a budget on a word boundary, so we never cut mid-token."""
    if len(text) <= budget:
        return text
    cut = text[:budget]
    space = cut.rfind(" ")
    return (cut[:space] if space > budget * 0.6 else cut).rstrip()


def build_search_document(scheme: dict[str, Any]) -> str:
    """One scheme -> the string we embed.

    `scheme` is a mapping of the `schemes` columns (a SQLAlchemy row works).
    """
    def val(key: str) -> Any:
        return scheme.get(key) if isinstance(scheme, dict) else getattr(scheme, key, None)

    def joined(key: str) -> str:
        items = val(key)
        return ", ".join(str(i) for i in items if i) if items else ""

    name = str(val("scheme_name") or "")
    short = str(val("scheme_short_title") or "")
    # The short title is usually the acronym people actually say, so it goes
    # next to the name rather than in a metadata tail.
    heading = f"{name} ({short})" if short and short.lower() not in name.lower() else name

    # Ordered most- to least-identifying. Labelled because a bare comma-joined
    # blob reads as noise to the model; "Category: Health & Wellness" gives it
    # something to align a query like "health scheme" against.
    lines = [heading]
    for label, text in (
        ("Category", joined("categories") or str(val("category") or "")),
        ("Subcategory", joined("subcategories")),
        ("Tags", joined("tags")),
        ("For", joined("target_beneficiaries")),
        ("Level", str(val("level") or "")),
        # "All"/nationwide is meaningful to a caller asking "is this for Bihar?"
        ("State", joined("states") or ("All India" if not val("states") else "")),
        ("Ministry", str(val("nodal_ministry") or "")),
        ("Benefit type", joined("benefit_types")),
    ):
        if text:
            lines.append(f"{label}: {text}")

    header = "\n".join(lines)

    # Prose last, and clipped: it is the part that would otherwise swamp the rest.
    remaining = MAX_DOC_CHARS - len(header) - 40
    body_parts = []
    for label, key in (("Description", "description"),
                       ("Details", "detailed_description"),
                       ("Benefits", "benefits_md"),
                       ("Eligibility", "eligibility_description")):
        text = val(key)
        if not text or remaining <= 0:
            continue
        text = " ".join(str(text).split())      # collapse markdown whitespace
        clipped = _clip(text, remaining)
        if clipped:
            body_parts.append(f"{label}: {clipped}")
            remaining -= len(clipped) + len(label) + 2

    return (header + "\n" + "\n".join(body_parts)).strip()


def build_query_text(
    query: str,
    *,
    state: str | None = None,
    category: str | None = None,
) -> str:
    """Query-side text.

    Hard filters are applied by Qdrant, not by the vector, so state/category are
    appended only as weak semantic hints — never as a substitute for the filter.
    Cross-cutting rule 3: a Bihar query must never return a Maharashtra scheme,
    and that guarantee comes from the filter, not from this string.
    """
    parts = [query.strip()]
    if category:
        parts.append(f"Category: {category}")
    if state:
        parts.append(f"State: {state}")
    return " ".join(p for p in parts if p)
