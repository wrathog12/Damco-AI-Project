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
    # The LLM's key is `gemini_api_key` below, not here: it is shared with the
    # embeddings and lives in the repo-root .env, while these two are the
    # backend's own.
    deepgram_api_key: str = ""
    cartesia_api_key: str = ""

    # ── Ingestion ───────────────────────────────────────
    # myScheme's x-api-key. Leave blank to let ingestion harvest it from the
    # live site via Playwright and cache it under /.cache/ — that also handles
    # rotation. Set it only to pin a known-good key.
    myscheme_api_key: str = ""

    # ── Google AI: embeddings *and* the LLM ─────────────
    # One key serves both `embedding_model` and `llm_model` — see the LLM block
    # below for why the conversation moved here from Groq. Billing is
    # pay-as-you-go, so the free tier's per-minute ceiling no longer applies.
    #
    # Google AI Studio key, read from the repo-root .env. Indexing and querying
    # MUST use the same model — vectors from two models are not comparable — so
    # changing `embedding_model` invalidates the whole Qdrant collection and
    # requires a full re-embed. `scheme_embedding_state.model` records which
    # model each row was embedded with so that is detectable rather than silent.
    gemini_api_key: str = ""
    embedding_model: str = "gemini-embedding-2"
    # Matryoshka truncation: the model emits a long vector that can be cut to a
    # shorter prefix. Smaller = less Qdrant memory, slightly worse recall.
    embedding_dim: int = 1536

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
    # See voice/transport.py — each option's own settings are below it.
    transport: Literal["smallwebrtc", "daily", "livekit"] = "smallwebrtc"

    # smallwebrtc: comma-separated STUN/TURN URLs. Empty is fine on localhost and
    # on most home networks; a symmetric-NAT or carrier-grade-NAT caller needs a
    # TURN server (coturn) here or the peer connection never completes.
    ice_servers: list[str] = ["stun:stun.l.google.com:19302"]

    daily_api_key: str = ""
    daily_room_url: str = ""

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    # ── Voice session behaviour ─────────────────────────
    # Replaces v1's `_inactivity_watcher` polling loop: Pipecat's worker raises
    # `on_idle_timeout` itself. 120s matches the old INACTIVITY_TIMEOUT.
    idle_timeout_secs: int = 120
    # How long a silence has to last before the turn is considered over. The
    # single most latency-sensitive knob in the pipeline: lower feels snappier
    # but cuts people off mid-thought, and callers reciting an address or a
    # scheme name pause a lot. P5 tunes this against real audio.
    vad_stop_secs: float = 0.4

    # ── LLM ─────────────────────────────────────────────
    # Authenticated with `gemini_api_key` above — the same key as embeddings.
    #
    # Groq is gone. Two independent reasons, and the second is the one that
    # actually forced it:
    #
    #  1. **Language matching.** `openai/gpt-oss-120b` answered plain English
    #     questions in Hinglish. Hardening the LANGUAGE block in the system
    #     prompt did not fix it, and neither did adding an English few-shot —
    #     the model simply generalised "Indian welfare scheme" to "reply in
    #     Hinglish". That is the single most-complained-about behaviour in this
    #     product, so it is not a defect we can prompt our way around.
    #  2. **Quota.** Groq's free tier is 8,000 tokens/minute, and one voice turn
    #     costs two-to-three calls each carrying the whole prompt. Two
    #     simultaneous callers could not both take a turn inside one minute, and
    #     the SDK retried the resulting 429s *silently* — surfacing as a 24-44s
    #     stall with nothing in the logs.
    #
    # Measured on the same fixed queries, `gemini-3.5-flash-lite` answers English
    # in English, Hindi in Hindi and Hinglish in Hinglish, keeps tool arguments in
    # English, and returns in 0.7-1.1s per call.
    #
    # Pinned, not `gemini-flash-lite-latest`: an alias silently changes the model
    # under a running service, and this one's exact tool-calling and
    # language-matching behaviour is what was verified. `gemini-2.5-flash-lite`
    # was measured too and is *not* a safe fallback — it echoed the prompt's
    # Hinglish example at English speakers, emitted Devanagari tool arguments, and
    # on one English query skipped the search entirely and invented scheme names.
    llm_model: str = "gemini-3.5-flash-lite"
    # Gemini counts thinking tokens against this budget, so the same trap as
    # gpt-oss applies: too small a cap and a turn that thinks hits the ceiling
    # mid-thought and returns nothing — no text, no tool call, silence on the
    # call. Reply length is disciplined by the system prompt, not by this.
    llm_max_tokens: int = 1024
    # Gemini 3 replaces gpt-oss's `reasoning_effort` with `thinking_level`, and
    # "minimal" is the correct setting for a voice agent for the same reason
    # "low" was: latency is the product. Pipecat would default a `gemini-3*flash*`
    # model to this anyway (see GoogleLLMService._maybe_unset_thinking_budget);
    # it is set explicitly so the value is visible and tunable here.
    #
    # NOTE: this is a *level*, not a token budget. The 2.5 series takes
    # `thinking_budget` instead and Gemini 3 may reject or ignore it, so
    # switching back to a 2.5 model means changing the field, not just the value.
    llm_thinking_level: str = "minimal"

    # ── TTS ─────────────────────────────────────────────
    tts_voice_id: str = "638efaaa-4d0c-442e-b701-3fae16aad012"  # Cartesia: Hindi Female
    # `sonic-2` was sunsetted — Cartesia now answers every request for it with
    # `400 Model sunsetted`, which silenced the agent completely. Still reachable
    # on this account: `sonic-3` (current flagship), `sonic-2-2025-04-16` (the
    # pinned snapshot of the old default) and `sonic-turbo` (lower latency, lower
    # quality — the one to measure against in P5).
    tts_model: str = "sonic-3"

    # ── STT ─────────────────────────────────────────────
    stt_model: str = "nova-3"
    stt_language: str = "hi"  # batch fallback's default when detection is absent
    # Nova-3's code-switching mode — one stream, any of the supported languages,
    # and it reports which one it heard. That report is the only language signal
    # the LLM gets (see voice/pipeline.py's LanguageTagger), so this must stay
    # "multi": pinning a single language silently makes the agent monolingual.
    stt_live_language: str = "multi"
    # Deepgram's streaming default is `endpointing=10`, i.e. finalise a
    # transcript after 10ms of silence. That is far shorter than the pause a
    # person leaves between words, so one sentence arrives as several finals —
    # and since the turn analyser closes the turn on its own clock, inference
    # can fire on the first fragment. Measured: "Bihar mein education scheme
    # dikhao" reached the LLM as just "बिहार", which it answered by searching for
    # health insurance. 300ms is Deepgram's own conversational recommendation.
    stt_endpointing_ms: int = 300
    # Deepgram's UtteranceEnd event, its slower backstop for "they have really
    # stopped". Needs interim_results, which is on.
    stt_utterance_end_ms: int = 1000

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
