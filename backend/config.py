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

    # ── Auth ────────────────────────────────────────────
    # `jwt_secret` also signs the anonymous-identity cookie (see auth/anon.py),
    # so rotating it logs everyone out AND resets every anonymous quota. That is
    # the correct trade — a leaked secret lets anyone mint unlimited free turns —
    # but it means rotation is a deliberate act, not a routine one.
    jwt_secret: str = "dev-only-insecure-change-me"
    jwt_algorithm: str = "HS256"
    # Short, because an access token cannot be revoked — it is checked by
    # signature alone, with no database read on the hot path. Fifteen minutes is
    # the window in which a leaked one is useful; the refresh token is the thing
    # that can actually be taken away.
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    # Rotated on every refresh and hashed at rest; see models/auth.py.
    refresh_cookie_name: str = "bh_refresh"

    # ── OTP login ───────────────────────────────────────
    # Phone OTP rather than email/password: the audience is welfare recipients on
    # cheap Android phones, most of whom have a number and no email they use.
    #
    # `stub` writes the code to the log instead of sending an SMS, which is what
    # makes the login flow testable without spending money — every real provider
    # charges per message. It is the default precisely so that forgetting to
    # configure a provider fails loudly in development rather than silently
    # sending nothing in production.
    otp_provider: Literal["stub", "msg91"] = "stub"
    otp_code_length: int = 6
    # Five minutes: long enough for a slow SMS, short enough that a code seen on
    # a lock screen is not a standing credential.
    otp_ttl_seconds: int = 300
    # Per challenge, not per phone — a wrong code burns one of five tries and the
    # sixth invalidates the challenge outright, so a six-digit code cannot be
    # walked through.
    otp_max_attempts: int = 5
    # Both are per phone number, and both exist because an SMS costs money: the
    # cooldown stops an impatient caller, the window cap stops a script.
    otp_resend_cooldown_seconds: int = 60
    otp_max_sends_per_window: int = 5
    otp_send_window_minutes: int = 60
    # Returns the code in the HTTP response so a test script can complete a login
    # without reading the log. Honoured **only** when `otp_provider == "stub"`, so
    # turning it on against a real provider does nothing. Never enable in a
    # deployment: it makes every account takeable by anyone who knows a number.
    otp_dev_echo: bool = False

    # MSG91 — chosen as the first real provider because it is Indian, prices in
    # rupees and does not require a US entity. Empty until someone buys credit.
    msg91_auth_key: str = ""
    msg91_template_id: str = ""

    # ── Profiles and memory (DPDP Act 2023) ─────────────
    # Deliberately no default, unlike `jwt_secret`. With this unset nothing
    # personal is stored at all: profiles refuse to save and transcripts are not
    # recorded. A well-known signing secret costs free turns; a well-known
    # *encryption* key means a database of caste and income that is encrypted in
    # form and public in fact, so refusing to collect is the safe failure. It is
    # also deliberately separate from `jwt_secret` — rotating that logs everyone
    # out, and coupling the two would make an ordinary logout-everyone rotation
    # destroy every stored profile. See auth/crypto.py.
    #
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    profile_encryption_key: str = ""
    # Bumped when the privacy notice changes, which requires every user to consent
    # again before their profile can be written to. Stored on the user row *and*
    # on the profile, so it is answerable which policy any given fact was
    # collected under.
    consent_version: str = "2026-09-1"

    # ── Retention ───────────────────────────────────────
    # "We delete old data" is a promise; a scheduled DELETE is a fact. The sweep
    # lives in services/retention.py and runs from the app's lifespan, so there is
    # no deployment where someone forgot to install a cron job.
    #
    # Transcripts go first and fastest — they are the most revealing thing here and
    # the least structured. Ninety days is long enough that a caller returning next
    # month is remembered and short enough that a leak is bounded.
    conversation_retention_days: int = 90
    # Which schemes someone looked at lives longer: it is the memory that makes a
    # second call better than a first, and it holds no sensitive category.
    interaction_retention_days: int = 365
    # Abandoned anonymous rows — a cookie cleared, a browser never reopened. This
    # is the prune `models/identity.py` promised when it accepted one dead row per
    # login on a new device. Only rows with no phone number are ever touched.
    anon_prune_days: int = 90
    # How often the sweep runs. Daily: the windows above are in days, so anything
    # finer only adds wake-ups.
    retention_sweep_hours: int = 24

    # ── Quota (anonymous-first access) ──────────────────
    # The product decision behind this: there is NO login wall. A first-time
    # caller talks to the agent immediately, because the audience is welfare
    # recipients on cheap phones and an OTP screen before any value is delivered
    # loses most of them. Login is what you do when you want *more*, not what you
    # do to start.
    #
    # Counted in conversation TURNS, not minutes or sessions. A turn is one
    # user utterance that reaches the LLM. Minutes would track cost more closely
    # (Deepgram, Cartesia and Gemini all bill by audio duration) — that is a known
    # trade, taken because turns are the unit a user can actually understand from
    # a spoken sentence, and because it is enforceable without the pipeline having
    # to report session duration on close.
    #
    # Both are per rolling window, per identity. Tune freely: they are the two
    # numbers most likely to change once there is real usage.
    anon_turn_quota: int = 10
    user_turn_quota: int = 100
    # The window these reset over. A rolling window, not a calendar day, so a
    # caller at 23:55 does not get a fresh allowance five minutes later.
    quota_window_hours: int = 24
    # Name of the anonymous-identity cookie. httpOnly and signed — see auth/anon.py.
    anon_cookie_name: str = "bh_anon"
    # Anonymous ids are cheap to mint and a caller who clears cookies gets a new
    # one. That is accepted: the free allowance is small enough that evading it is
    # more effort than logging in, and the alternative (keying quota on IP) makes
    # a personal identifier out of something DPDP then obliges us to justify,
    # disclose and expire — for a bucket of ten turns.
    anon_cookie_ttl_days: int = 365

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
