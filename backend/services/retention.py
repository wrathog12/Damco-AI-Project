"""
The retention sweep — the difference between a policy and a promise.

The DPDP Act 2023 asks for a documented retention policy. A document is not a
control: the control is a `DELETE` that runs whether or not anyone remembers it.
So the windows live in `config.py`, this module enforces them, and
`main.py`'s lifespan starts the loop — there is no deployment in which someone
forgot to install the cron job, and no environment where the policy is true in
staging and aspirational in production.

## The windows, and why they differ

| What | Window | Why |
|---|---|---|
| `conversations` + `messages` | `conversation_retention_days` (90) | The transcript is the most revealing and least structured thing here. A caller explaining why they need help says more in one sentence than the whole profile records. Ninety days remembers someone who comes back next month and bounds what a leak could contain. |
| `scheme_interactions` | `interaction_retention_days` (365) | Which schemes someone looked at holds no sensitive category and is what makes the second call better than the first. |
| anonymous `users` | `anon_prune_days` (90) | A cleared cookie leaves a row nobody can ever reach again. This is the prune `models/identity.py` promised when it accepted one dead row per login on a new device. |
| expired `otp_challenges` | immediate | A phone number's purpose ends when the code is verified or expires. `services/auth.py` already sweeps on send; this catches a number nobody ever texted again. |
| spent `refresh_tokens` | past `expires_at` | A revoked or expired token can authenticate nothing, so keeping the hash is retention without a purpose. |

**`user_profiles` and `eligibility_evaluations` are not swept, and that is the
policy, not an omission.** A profile is not a log of an event that ages out; it is
the thing the caller asked us to remember so they would not have to say it again.
Deleting it on a timer would silently undo the feature they consented to. It goes
when they erase it or close their account, both of which are one request away — and
the anonymous prune above means a profile can never outlive an account nobody can
reach, because a row with a profile has a phone number by construction.

## Two properties this code must keep

**Nothing here touches a row with a phone number.** The user prune's `WHERE` clause
is `phone_e164 IS NULL`, and that condition is the entire safety of the operation:
without it, the sweep is a routine that deletes accounts.

**It never raises into the caller.** `purge` is run from a background task in the
app's lifespan; an exception there would kill the loop for the life of the process
and take the policy with it silently. So each step is independent and a failure is
logged and counted, not propagated.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import delete, func, or_, select

from config import settings
from models.auth import OtpChallenge, RefreshToken
from models.identity import User
from models.profile import Conversation, SchemeInteraction
from services.resources import Resources


def policy() -> dict[str, int]:
    """The windows in force, for `/health`, `check_config.py` and the notice text.

    A privacy notice that states a number the code does not enforce is worse than
    one that states nothing, so both read it from here.
    """
    return {
        "conversation_days": settings.conversation_retention_days,
        "interaction_days": settings.interaction_retention_days,
        "anonymous_identity_days": settings.anon_prune_days,
        "sweep_every_hours": settings.retention_sweep_hours,
    }


async def purge(res: Resources) -> dict[str, int]:
    """Apply every window once. Returns per-table counts; never raises."""
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}

    async def step(label: str, statement) -> None:
        # A separate session per step, so one failure cannot roll back the work
        # the previous steps already committed.
        try:
            async with res.session_factory() as session:
                result = await session.execute(statement)
                await session.commit()
            counts[label] = result.rowcount or 0
        except Exception as exc:                                # noqa: BLE001
            counts[label] = -1
            logger.error(f"[retention] {label} sweep failed: "
                         f"{type(exc).__name__}: {exc}")

    # Transcripts first: shortest window, most sensitive content. `messages` goes
    # with them by cascade, which is also what keeps this a single statement.
    await step("conversations", delete(Conversation).where(
        Conversation.started_at
        < now - timedelta(days=settings.conversation_retention_days)))

    await step("interactions", delete(SchemeInteraction).where(
        SchemeInteraction.created_at
        < now - timedelta(days=settings.interaction_retention_days)))

    await step("otp_challenges", delete(OtpChallenge).where(
        OtpChallenge.expires_at < now))

    # A token past its expiry authenticates nothing. A *revoked* one is kept until
    # it would have expired anyway, deliberately: `family_id` reuse detection is
    # what makes refresh-token theft visible, and deleting the evidence the moment
    # a family is revoked would make the second presentation look like an unknown
    # token rather than a replay.
    await step("refresh_tokens", delete(RefreshToken).where(
        RefreshToken.expires_at < now))

    await step("anonymous_users", delete(User).where(
        # The safety condition. Never widen this without reading the module
        # docstring — without it this statement deletes accounts.
        User.phone_e164.is_(None),
        # `last_seen_at` is only written on activity, so `created_at` is the
        # fallback for a row that was minted and never used again.
        func.coalesce(User.last_seen_at, User.created_at)
        < now - timedelta(days=settings.anon_prune_days),
    ))

    swept = {k: v for k, v in counts.items() if v}
    if swept:
        logger.info(f"[retention] swept {swept}")
    else:
        logger.debug("[retention] nothing to sweep")
    return counts


async def pending(res: Resources) -> dict[str, int]:
    """How many rows are currently past their window. For verification only.

    `verify_profiles.py` uses this to prove the sweep did something rather than
    trusting a count of zero that would also be produced by a sweep that never
    ran.
    """
    now = datetime.now(timezone.utc)
    async with res.session_factory() as session:
        conversations = (await session.execute(
            select(func.count()).select_from(Conversation)
            .where(Conversation.started_at
                   < now - timedelta(days=settings.conversation_retention_days)))
        ).scalar() or 0
        interactions = (await session.execute(
            select(func.count()).select_from(SchemeInteraction)
            .where(SchemeInteraction.created_at
                   < now - timedelta(days=settings.interaction_retention_days)))
        ).scalar() or 0
        anon = (await session.execute(
            select(func.count()).select_from(User)
            .where(User.phone_e164.is_(None),
                   func.coalesce(User.last_seen_at, User.created_at)
                   < now - timedelta(days=settings.anon_prune_days)))
        ).scalar() or 0
        tokens = (await session.execute(
            select(func.count()).select_from(RefreshToken)
            .where(or_(RefreshToken.expires_at < now)))).scalar() or 0
    return {"conversations": conversations, "interactions": interactions,
            "anonymous_users": anon, "refresh_tokens": tokens}


async def run_forever(res: Resources) -> None:
    """The sweep loop the app's lifespan starts. Cancelled on shutdown.

    Runs once at startup rather than waiting a full interval first: a process that
    restarts more often than `retention_sweep_hours` would otherwise never sweep
    at all, which is exactly the failure mode of a policy nobody checks.
    """
    interval = max(1, settings.retention_sweep_hours) * 3600
    while True:
        try:
            await purge(res)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                # noqa: BLE001
            # `purge` already swallows per-step failures, so reaching here means
            # something structural. Still not fatal: the loop is the policy.
            logger.error(f"[retention] sweep loop error: "
                         f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(interval)
