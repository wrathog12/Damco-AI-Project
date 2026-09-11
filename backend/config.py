"""
Configuration — loads .env and exposes typed settings.

Every path here is resolved from *this file's* location, not the process
working directory, so `python backend/main.py` and `cd backend && python main.py`
behave identically.

Two .env files are read, in increasing order of priority:
  1. <repo root>/.env   — infrastructure (docker compose reads the same file)
  2. backend/.env       — application secrets (API keys)
"""
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── API Keys ────────────────────────────────────────
    groq_api_key: str = ""
    deepgram_api_key: str = ""
    cartesia_api_key: str = ""

    # ── Ingestion ───────────────────────────────────────
    # myScheme's x-api-key. Leave blank to let ingestion harvest it from the
    # live site via Playwright and cache it under /.cache/ — that also handles
    # rotation. Set it only to pin a known-good key.
    myscheme_api_key: str = ""

    # ── Knowledge base (legacy JSON — superseded by Postgres in P1) ──
    kb_path: str = str(REPO_ROOT / "scraped_data" / "knowledge_base_translated.json")

    # ── Postgres ────────────────────────────────────────
    # Async driver for the app; Alembic swaps in psycopg2 itself.
    database_url: str = "postgresql+asyncpg://bhasha:bhasha@localhost:5432/bhasha"

    # ── Qdrant ──────────────────────────────────────────
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "schemes"

    # ── Auth (used from P3; declared now so config stays in one place) ──
    jwt_secret: str = "dev-only-insecure-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    # ── Voice transport ─────────────────────────────────
    # Keeps the hosting decision open: swap without touching pipeline code.
    transport: Literal["smallwebrtc", "daily", "livekit"] = "smallwebrtc"

    # ── LLM ─────────────────────────────────────────────
    llm_model: str = "llama-3.3-70b-versatile"

    # ── TTS ─────────────────────────────────────────────
    tts_voice_id: str = "638efaaa-4d0c-442e-b701-3fae16aad012"  # Cartesia: Hindi Female
    tts_model: str = "sonic-2"

    # ── STT ─────────────────────────────────────────────
    stt_model: str = "nova-3"
    stt_language: str = "hi"  # primary language hint

    # ── Server ──────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    # Comma-separated in .env, e.g. CORS_ORIGINS=http://localhost:3000,https://app.example.com
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    @property
    def sync_database_url(self) -> str:
        """Same database, psycopg2 driver — Alembic runs migrations synchronously."""
        return self.database_url.replace("+asyncpg", "+psycopg2")


settings = Settings()
