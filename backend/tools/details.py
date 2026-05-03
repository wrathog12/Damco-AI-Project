"""
get_scheme_details tool — return full scheme data for LLM to speak about.
"""
import json
from knowledge import loader


def get_scheme_details(scheme_id: str) -> str:
    """
    Look up a scheme by ID and return its full JSON.
    The LLM uses this to answer detailed questions about a scheme.
    """
    scheme = loader.get_by_id(scheme_id)

    if not scheme:
        return json.dumps({"error": f"Scheme '{scheme_id}' not found."})

    # ── Build a COMPACT summary for the LLM (~300 tokens max) ──
    # The LLM only needs enough to speak 2-3 sentences.
    # Full details (documents, application process, translations) are
    # rendered by the frontend card popup, which reads the KB directly.

    # Truncate description to first 150 chars
    desc = scheme.get("description", "")
    short_desc = (desc[:150] + "…") if len(desc) > 150 else desc

    # Top 2 benefits only (amount + short description)
    raw_benefits = scheme.get("benefits", [])
    compact_benefits = []
    for b in raw_benefits[:2]:
        entry = {}
        if b.get("amount"):
            entry["amount"] = b["amount"]
        bd = b.get("description", "")
        entry["description"] = (bd[:80] + "…") if len(bd) > 80 else bd
        compact_benefits.append(entry)

    # One-liner eligibility
    elig_desc = scheme.get("eligibility_description", "")
    short_elig = (elig_desc[:150] + "…") if len(elig_desc) > 150 else elig_desc

    return json.dumps({
        "scheme_id": scheme.get("scheme_id"),
        "scheme_name": scheme.get("scheme_name"),
        "state": scheme.get("metadata", {}).get("state"),
        "category": scheme.get("metadata", {}).get("category"),
        "description": short_desc,
        "top_benefits": compact_benefits,
        "eligibility_summary": short_elig,
        "has_application_url": bool(scheme.get("application_url")),
    }, ensure_ascii=False)
