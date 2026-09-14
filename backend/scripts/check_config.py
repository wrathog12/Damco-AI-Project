"""
Smoke-check the configuration and infrastructure wiring.

Prints resolved (non-secret) settings and probes Postgres + Qdrant.
Run from anywhere:  python backend/scripts/check_config.py
"""
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import settings  # noqa: E402


def _redact(url: str) -> str:
    """Hide the password in a DSN before printing it."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def check_settings() -> bool:
    print("-- Settings -------------------------------------")
    print(f"  database_url    {_redact(settings.database_url)}")
    print(f"  qdrant_url      {settings.qdrant_url}")
    print(f"  qdrant_coll     {settings.qdrant_collection}")
    print(f"  transport       {settings.transport}")
    print(f"  cors_origins    {settings.cors_origins}")
    print(f"  llm_model       {settings.llm_model}")
    print(f"  embedding_model {settings.embedding_model} ({settings.embedding_dim}d)")

    ok = True

    # `gemini_api_key` authenticates the LLM *and* the embeddings, so a missing
    # one takes down both the conversation and retrieval. It comes from the
    # repo-root .env, unlike the other two, which is exactly the mistake this
    # check exists to catch.
    for name in ("gemini_api_key", "deepgram_api_key", "cartesia_api_key"):
        present = bool(getattr(settings, name))
        print(f"  {name:<15} {'set' if present else 'NOT SET'}")
        ok &= present

    # `jwt_secret` signs three things now: the anonymous cookie, the access JWT
    # and the OTP code hashes. Rotating it therefore logs everyone out, resets
    # every anonymous quota AND invalidates every code in flight.
    if settings.jwt_secret == "dev-only-insecure-change-me":
        print("  jwt_secret      default (fine locally, must change to deploy)")
    elif len(settings.jwt_secret.encode()) < 32:
        # PyJWT warns about this on every mint. RFC 7518 §3.2 wants a key at least
        # as long as the hash output for HS256, and a short one is guessable.
        print(f"  jwt_secret      set but only "
              f"{len(settings.jwt_secret.encode())} bytes — use 32+ "
              f"(`python -c \"import secrets;print(secrets.token_urlsafe(32))\"`)")
    else:
        print("  jwt_secret      set")

    # No default on purpose (see auth/crypto.py): with this unset, profiles and
    # transcripts are not stored at all. That is the safe failure, but it is a
    # silent one from the outside — the app answers every request normally and
    # simply remembers nothing — so it is reported here rather than only warned
    # about once at startup.
    from auth import crypto  # local: importing it derives and caches the key

    if crypto.available():
        # A fingerprint, never the key. Six hex characters is enough to tell "the
        # key changed" from "the data is corrupt" and useless to anyone else.
        print(f"  profile_key     set (fingerprint {crypto.fingerprint()}, "
              f"{len(settings.profile_encryption_key.encode())} bytes)")
    else:
        print("  profile_key     NOT SET — profiles and transcripts are not "
              "stored")
        print("                  set PROFILE_ENCRYPTION_KEY to enable "
              "(`python -c \"import secrets;print(secrets.token_urlsafe(48))\"`)")
    print(f"  consent_version {settings.consent_version}")
    print(f"  retention       transcripts {settings.conversation_retention_days}d, "
          f"interactions {settings.interaction_retention_days}d, "
          f"anon ids {settings.anon_prune_days}d, "
          f"sweep every {settings.retention_sweep_hours}h")

    print(f"  otp_provider    {settings.otp_provider}")
    if settings.otp_dev_echo:
        # Loud, because with the stub provider this hands a code to anyone who
        # asks for one — every account becomes takeable by phone number alone.
        state = ("ACTIVE — codes are returned over HTTP"
                 if settings.otp_provider == "stub"
                 else "set but ignored (provider is not the stub)")
        print(f"  otp_dev_echo    {state}")

    return bool(ok)


async def check_postgres() -> bool:
    print("\n-- Postgres -------------------------------------")
    try:
        from sqlalchemy import text

        from db import dispose_engine, session_scope

        async with session_scope() as session:
            version = (await session.execute(text("SELECT version()"))).scalar_one()
            # `alembic_version` does not exist until the first upgrade runs, and
            # that is a normal state — not an error. to_regclass returns NULL
            # instead of raising, which would otherwise abort the transaction.
            table_exists = (
                await session.execute(text("SELECT to_regclass('alembic_version')"))
            ).scalar_one() is not None
            revision = (
                (await session.execute(text("SELECT version_num FROM alembic_version")))
                .scalars()
                .all()
                if table_exists
                else []
            )
        await dispose_engine()
        print(f"  connected: {version.split(',')[0]}")
        print(f"  alembic revision: {revision or 'none applied yet (run: alembic upgrade head)'}")
        return True
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        print("  -> is `docker compose up -d` running?")
        return False


def check_qdrant() -> bool:
    print("\n-- Qdrant ---------------------------------------")
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
        )
        names = [c.name for c in client.get_collections().collections]
        print(f"  connected: {settings.qdrant_url}")
        print(f"  collections: {names or 'none yet'}")
        return True
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        print("  -> is `docker compose up -d` running?")
        return False


async def main() -> int:
    results = [check_settings(), await check_postgres(), check_qdrant()]
    print()
    if all(results):
        print("All checks passed.")
        return 0
    print("Some checks failed - see above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
