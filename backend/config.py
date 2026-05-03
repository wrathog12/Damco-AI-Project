"""
Configuration — loads .env and exposes typed settings.
"""
from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # ── API Keys ────────────────────────────────────────
    groq_api_key: str = ""
    deepgram_api_key: str = ""
    cartesia_api_key: str = ""

    # ── Knowledge base ──────────────────────────────────
    kb_path: str = str(Path(__file__).resolve().parent.parent / "scraped_data" / "knowledge_base_translated.json")

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

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
