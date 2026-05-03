"""
Knowledge base loader — reads knowledge_base_raw.json into memory once on startup.
"""
import json
from pathlib import Path
from typing import Optional

# Global in-memory store
_schemes: list[dict] = []
_schemes_by_id: dict[str, dict] = {}


def load(kb_path: str) -> int:
    """
    Load the knowledge base JSON file into memory.
    Returns the number of schemes loaded.
    """
    global _schemes, _schemes_by_id

    path = Path(kb_path)
    if not path.exists():
        raise FileNotFoundError(f"Knowledge base not found: {kb_path}")

    with open(path, "r", encoding="utf-8") as f:
        kb = json.load(f)

    _schemes = kb.get("schemes", [])

    # Build lookup index by scheme_id
    _schemes_by_id = {}
    for s in _schemes:
        sid = s.get("scheme_id")
        if sid:
            _schemes_by_id[sid] = s

    print(f"[KB] Loaded {len(_schemes)} schemes from {path.name}")
    return len(_schemes)


def get_all() -> list[dict]:
    """Return all loaded schemes."""
    return _schemes


def get_by_id(scheme_id: str) -> Optional[dict]:
    """Look up a single scheme by its ID."""
    return _schemes_by_id.get(scheme_id)


def get_stats() -> dict:
    """Return summary stats about the loaded KB."""
    states = set()
    categories = set()
    for s in _schemes:
        states.add(s.get("metadata", {}).get("state", "Unknown"))
        categories.add(s.get("metadata", {}).get("category", "Unknown"))
    return {
        "total_schemes": len(_schemes),
        "states": sorted(states),
        "categories": sorted(categories),
    }
