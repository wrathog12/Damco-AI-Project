"""
Turn accounting: who gets to speak, and how many more times.

The rule this implements: an anonymous caller gets `anon_turn_quota` turns per
window, an authenticated one gets `user_turn_quota`, and nobody gets a login
screen before their first turn.

## Why the counter is a single UPDATE and not read-then-write

`consume()` is called on **every turn of a live voice call**, and two turns can
be in flight at once — a caller on two tabs, or a text `/chat` request racing a
voice turn. Read-modify-write would let both observe `turns_used = 9` and both
write 10, so the eleventh turn of a ten-turn allowance is free. Rather than take
a row lock and hold it across the pipeline's awaits, the whole decision is one
atomic statement:

* the `WHERE` clause is the *authorisation* — it matches only if the window has
  expired or there is allowance left, so a row that comes back means the turn was
  granted, and no row means it was denied;
* the `CASE` expressions are the *rollover* — an expired window resets the count
  to 1 in the same breath, so there is no separate sweep job and no moment where a
  stale window looks like an exhausted one.

Postgres evaluates `now()` once per statement, so the window boundary cannot move
between the check and the write.

## Rollover is anchored to first use, not to midnight

`quota_window_started_at` is set on the caller's first counted turn. A calendar
reset would hand someone who ran out at 23:55 a fresh allowance five minutes
later; anchoring to first use means everyone actually experiences the full
`quota_window_hours`. The cost is that windows are per-caller, so there is no
single instant when the whole system resets — which is a feature under load.

NULL means "never counted a turn", which is deliberately distinct from an expired
window: the first is a new caller, the second a returning one. Conflating them
would be harmless today and wrong the moment anything reports on activation.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.identity import User
from services.resources import Resources


@dataclass(frozen=True)
class Verdict:
    """The outcome of asking for one turn.

    `allowed` is the only field the pipeline must respect. The rest exist so the
    agent can say something *specific* — "you have two turns left" reads as a
    service, "quota exceeded" reads as a failure.
    """
    allowed: bool
    turns_used: int
    limit: int
    authenticated: bool
    # When the current window expires, so the caller can be told when they are
    # welcome back. None before any turn has ever been counted.
    resets_at: datetime | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.turns_used)

    @property
    def is_last_turn(self) -> bool:
        """True when this turn was granted and it was the final one.

        The pipeline uses this to warn *during* the last answer rather than
        refusing the next one cold — being cut off without notice is the part
        users experience as broken, not the limit itself.
        """
        return self.allowed and self.remaining == 0


def limit_for(user: User) -> int:
    """The allowance this identity gets. Authentication is the only lever."""
    return (settings.user_turn_quota if user.phone_e164
            else settings.anon_turn_quota)


async def get_or_create_anon(res: Resources, anon_id: str) -> User:
    """Find the row for a signed anonymous id, creating it on first contact.

    The insert races itself whenever a caller opens two tabs at once, and the
    unique index on `anon_id` is what makes that safe: the loser of the race
    catches `IntegrityError` and re-reads rather than failing the request. A
    caller must never be denied service because they double-clicked.
    """
    async with res.session_factory() as session:
        user = await _by_anon_id(session, anon_id)
        if user is not None:
            return user

        user = User(anon_id=anon_id, last_seen_at=datetime.now(timezone.utc))
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            user = await _by_anon_id(session, anon_id)
            if user is None:                       # pragma: no cover — unreachable
                raise
            return user
        await session.refresh(user)
        return user


async def _by_anon_id(session: AsyncSession, anon_id: str) -> User | None:
    return (await session.execute(
        select(User).where(User.anon_id == anon_id))).scalar_one_or_none()


# One statement: authorise, roll the window over, and count the turn.
#
# The window boundary is computed in Postgres, not in Python: a cutoff read on the
# app's clock and compared against a row the database may have changed since would
# reintroduce the race this statement exists to remove. `now()` is evaluated once
# per statement, so every mention of `w.cutoff` below is the same instant.
#
# It arrives as `:hours` through `make_interval` rather than as a `timedelta`
# bound to `now() - :window`. That form fails at runtime: with nothing to pin the
# parameter's type, Postgres resolves `now() - $1` as timestamptz minus
# timestamptz, and the comparison then reads `timestamp with time zone < interval`
# — `operator does not exist`. An integer and `make_interval` leave nothing to
# infer.
_CONSUME = text("""
    UPDATE users
       SET turns_used = CASE
               WHEN users.quota_window_started_at IS NULL
                 OR users.quota_window_started_at < w.cutoff
               THEN 1
               ELSE users.turns_used + 1
           END,
           quota_window_started_at = CASE
               WHEN users.quota_window_started_at IS NULL
                 OR users.quota_window_started_at < w.cutoff
               THEN now()
               ELSE users.quota_window_started_at
           END,
           last_seen_at = now()
      FROM (SELECT now() - make_interval(hours => :hours) AS cutoff) AS w
     WHERE users.id = :user_id
       AND (users.quota_window_started_at IS NULL
            OR users.quota_window_started_at < w.cutoff
            OR users.turns_used < :limit)
 RETURNING users.turns_used, users.quota_window_started_at
""")


async def consume(res: Resources, user: User) -> Verdict:
    """Spend one turn if there is one to spend. Atomic; never raises on denial.

    A denial is an ordinary outcome, not an error — the caller is mid-conversation
    and the agent has to say something graceful. Callers get a `Verdict`, and the
    voice path turns a denied one into a spoken line plus an armed `CallCloser`
    rather than a dropped connection.
    """
    limit = limit_for(user)
    window = timedelta(hours=settings.quota_window_hours)

    async with res.session_factory() as session:
        row = (await session.execute(_CONSUME, {
            "user_id": user.id, "limit": limit,
            "hours": settings.quota_window_hours,
        })).first()
        await session.commit()

    if row is None:
        # Denied. Re-read for an accurate `resets_at` to tell the caller — this is
        # the one path where an extra query is worth it, because the number is
        # about to be spoken out loud.
        state = await peek(res, user)
        logger.info(f"[quota] denied user={user.id} "
                    f"limit={limit} auth={bool(user.phone_e164)}")
        return Verdict(allowed=False, turns_used=state.turns_used, limit=limit,
                       authenticated=bool(user.phone_e164),
                       resets_at=state.resets_at)

    turns_used, window_started = row
    # Keep the in-memory object honest: it is held for the life of the session and
    # anything reading `user.turns_used` afterwards would otherwise see the value
    # from before this turn.
    user.turns_used = turns_used
    user.quota_window_started_at = window_started

    return Verdict(allowed=True, turns_used=turns_used, limit=limit,
                   authenticated=bool(user.phone_e164),
                   resets_at=window_started + window if window_started else None)


async def peek(res: Resources, user: User) -> Verdict:
    """Report the allowance without spending any of it.

    Used by `GET /api/quota` and by the greeting path, where consuming a turn to
    find out whether a turn is available would be its own bug. An expired window
    is reported as fully available rather than as used up, matching what `consume`
    would do next.
    """
    limit = limit_for(user)
    window = timedelta(hours=settings.quota_window_hours)

    async with res.session_factory() as session:
        row = (await session.execute(
            select(User.turns_used, User.quota_window_started_at)
            .where(User.id == user.id))).first()

    if row is None:                                 # deleted mid-session
        return Verdict(allowed=False, turns_used=limit, limit=limit,
                       authenticated=bool(user.phone_e164))

    turns_used, window_started = row
    expired = (window_started is None
               or window_started < datetime.now(timezone.utc) - window)
    if expired:
        return Verdict(allowed=True, turns_used=0, limit=limit,
                       authenticated=bool(user.phone_e164))

    return Verdict(allowed=turns_used < limit, turns_used=turns_used, limit=limit,
                   authenticated=bool(user.phone_e164),
                   resets_at=window_started + window)
