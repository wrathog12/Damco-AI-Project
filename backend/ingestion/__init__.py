"""
Ingestion — pulls the national scheme corpus from myScheme's JSON API.

This replaces the v1 pipeline (Playwright render + Gemini extraction of every
page). The API supplies structured fields plus Markdown bodies directly, and
serves native hi/bn/mr/ta translations, so neither the browser nor the LLM is
needed to acquire content.

Two stages, kept separate on purpose:
  harvest  — API -> raw JSON on disk        (this package, `harvest.py`)
  load     — raw JSON -> canonical Postgres (this package, `normalize.py`/`load.py`)

Embedding into Qdrant is a *third*, independent pass that reads Postgres, so
re-running it never re-hits the API.
"""
from .client import (MAX_PAGE_SIZE, MySchemeClient, PermanentApiError,
                     SchemeApiError, language_of)
from .harvest import DEFAULT_LANGS, Harvester

__all__ = [
    "MySchemeClient", "SchemeApiError", "PermanentApiError", "language_of",
    "MAX_PAGE_SIZE", "Harvester", "DEFAULT_LANGS",
]
