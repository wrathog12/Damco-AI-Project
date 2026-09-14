"""
The login flows: send a code, verify it, rotate a session, end one.

Everything here touches Postgres and nothing here knows about HTTP beyond the
status code an `AuthError` carries. `auth/` holds the primitives (hashing,
signing, phone normalisation); this module holds the decisions.

## Login is the one place this system says no

The rest of the request path never refuses a caller: `current_user` always
succeeds, `/chat` answers an exhausted quota with a 200, and a voice caller with
nothing left still gets a connection. That is a product rule about *service*, and
it does not extend to credentials. A wrong code is a wrong code, and a 401 is the
honest answer — the caller is not asking for the service, they are asking to be
believed about who they are.

So these are the endpoints that return 400/401/429, and they are the only ones.

## Promotion, and why it is not the merge the schema rules out

`models/identity.py` says login abandons the anonymous row rather than merging it.
That holds, and it is about the case where **both** rows exist: a returning caller
whose phone already has a row, signing in on a new device, where two counters
would have to become one and neither answer is right.

A first login is not that case. There is no phone row yet, so the anonymous row
simply *acquires* a phone number — one row, one counter, nothing combined. That is
strictly better than minting a second row: no dead row, and the turns already spent
this window carry over, which is correct because it is the same person in the same
window. Someone who burns ten anonymous turns and then signs in gets the remaining
ninety, not a fresh hundred, and not a lockout.

The distinction is worth holding onto: **promote when the phone is new, abandon
when the phone already has a row.**
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import otp, tokens
from auth.phone import to_e164
from config import settings
from models.auth import OtpChallenge, RefreshToken
from models.identity import User
from services.resources import Resources


class AuthError(Exception):
    """A login attempt that failed, with the status it should become.

    Carries a machine-readable `code` alongside the message because a client has
    to react differently to `otp_cooldown` (show a timer) and `otp_bad_code`
    (clear the field), and matching on English prose is how that breaks the first
    time the wording is improved.
    """

    def __init__(self, status: int, code: str, detail: str,
                 *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.retry_after = retry_after


@dataclass(frozen=True)
class Session:
    """What a completed login hands back."""
    user: User
    access_token: str
    expires_in: int
    # The plaintext refresh token, for the endpoint to put in a cookie. It exists
    # in this object and in that cookie, and nowhere else — the database has only
    # its hash.
    refresh_token: str


def normalise_phone(raw: str | None) -> str:
    """Validate and canonicalise, or raise the 400."""
    phone = to_e164(raw)
    if phone is None:
        raise AuthError(400, "phone_invalid",
                        "That does not look like a mobile number. Please enter a "
                        "10-digit Indian mobile number.")
    return phone


# ── 1. request a code ───────────────────────────────────
async def request_otp(res: Resources, raw_phone: str) -> tuple[str, str | None]:
    """Send a code to a phone. Returns `(phone_e164, code_if_echoing)`.

    The order here is the whole security of the endpoint: sweep, rate-limit,
    **send**, then write the row. Writing the challenge before the send would
    rate-limit a caller for a message that never left, and sending before the
    rate-limit check would let a loop spend real money.
    """
    phone = normalise_phone(raw_phone)
    now = datetime.now(timezone.utc)

    async with res.session_factory() as session:
        # Retention, not housekeeping: an expired challenge is a phone number the
        # purpose for collecting has ended for. Done here rather than on a
        # schedule so there is no deployment where the sweep was never set up.
        await session.execute(
            delete(OtpChallenge).where(OtpChallenge.expires_at < now))

        await _check_send_limits(session, phone, now)
        await session.commit()

    code = otp.generate_code()
    try:
        await otp.get_sender().send(phone, code)
    except Exception as exc:                                     # noqa: BLE001
        # 502, not 500: the fault is a provider we depend on, and the caller's
        # correct response is to try again rather than to check their input.
        logger.error(f"[otp] send failed via {settings.otp_provider}: "
                     f"{type(exc).__name__}: {exc}")
        raise AuthError(502, "otp_send_failed",
                        "We could not send the code just now. Please try again.")

    async with res.session_factory() as session:
        session.add(OtpChallenge(
            phone_e164=phone,
            code_hash=otp.hash_code(phone, code),
            expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
            created_at=now,
        ))
        await session.commit()

    return phone, (code if otp.dev_echo_enabled() else None)


async def _check_send_limits(session: AsyncSession, phone: str,
                             now: datetime) -> None:
    """Cooldown and window cap, both keyed on the phone number.

    Keyed on the phone and not on the caller's identity or IP, deliberately. The
    thing being protected is the SMS bill and the person whose phone would ring,
    and both of those belong to the number — an attacker with a fresh cookie per
    request would sail past a per-identity limit while the victim's phone buzzed
    every second. (IP is not an option here for the reason in `auth/anon.py`.)
    """
    latest = (await session.execute(
        select(func.max(OtpChallenge.created_at))
        .where(OtpChallenge.phone_e164 == phone))).scalar_one_or_none()

    if latest is not None:
        elapsed = (now - latest).total_seconds()
        if elapsed < settings.otp_resend_cooldown_seconds:
            wait = int(settings.otp_resend_cooldown_seconds - elapsed) + 1
            raise AuthError(429, "otp_cooldown",
                            f"Please wait {wait} seconds before asking for "
                            f"another code.", retry_after=wait)

    window_start = now - timedelta(minutes=settings.otp_send_window_minutes)
    sent = (await session.execute(
        select(func.count()).select_from(OtpChallenge)
        .where(OtpChallenge.phone_e164 == phone,
               OtpChallenge.created_at >= window_start))).scalar_one()

    if sent >= settings.otp_max_sends_per_window:
        raise AuthError(429, "otp_send_limit",
                        "Too many codes requested for this number. Please try "
                        "again later.",
                        retry_after=settings.otp_send_window_minutes * 60)


# ── 2. verify it ────────────────────────────────────────
async def verify_otp(res: Resources, raw_phone: str, code: str,
                     caller: User) -> Session:
    """Check a code and, if it is right, hand back a logged-in session.

    `caller` is whoever `current_user` resolved for this request — normally the
    anonymous identity that has been talking to the agent. It is what makes
    promotion possible: without it, a first login would have to create a second row
    and the caller's turns-used history would be silently forgotten.
    """
    phone = normalise_phone(raw_phone)
    now = datetime.now(timezone.utc)

    async with res.session_factory() as session:
        challenge = (await session.execute(
            select(OtpChallenge)
            .where(OtpChallenge.phone_e164 == phone,
                   OtpChallenge.expires_at > now)
            .order_by(OtpChallenge.created_at.desc())
            .limit(1)
            # The row is about to be counted against and possibly deleted, and two
            # tabs submitting at once must not both spend the same attempt.
            .with_for_update())).scalars().first()

        if challenge is None:
            raise AuthError(401, "otp_expired",
                            "That code has expired. Please request a new one.")

        if not otp.matches(phone, code.strip(), challenge.code_hash):
            challenge.attempts += 1
            burned = challenge.attempts >= settings.otp_max_attempts
            if burned:
                # Delete rather than just refuse: leaving an exhausted challenge
                # in place would make the *next* verify report "expired" against a
                # row that is still live, and the caller needs a new code anyway.
                await session.delete(challenge)
            await session.commit()
            logger.info(f"[otp] wrong code, attempt {challenge.attempts}"
                        f"{' (challenge burned)' if burned else ''}")
            raise AuthError(401, "otp_bad_code",
                            "That code is not right. Please check and try again."
                            if not burned else
                            "Too many wrong attempts. Please request a new code.")

        # Right code. Every challenge for this number goes, which both consumes
        # this one and invalidates any older code still sitting in an inbox.
        await session.execute(
            delete(OtpChallenge).where(OtpChallenge.phone_e164 == phone))
        user = await _login_row(session, phone, caller)
        await session.commit()

    logger.info(f"[auth] login ok, user={user.id}")
    return await _issue_session(res, user)


async def _login_row(session: AsyncSession, phone: str, caller: User) -> User:
    """The `User` this phone number logs in as. See the module docstring.

    Runs inside the caller's transaction so the challenge deletion and the
    identity decision commit together — a promotion that succeeded while the code
    stayed valid would be a code usable twice.
    """
    existing = (await session.execute(
        select(User).where(User.phone_e164 == phone))).scalars().first()
    if existing is not None:
        if existing.id != caller.id:
            # The anonymous row is left behind, not deleted: `last_seen_at` will
            # let it be pruned, and deleting it here would destroy the only record
            # of turns spent by a caller who might not have finished their call.
            logger.info(f"[auth] phone already known; anonymous row "
                        f"{caller.id} abandoned for {existing.id}")
        return existing

    # Promotion, guarded in SQL rather than by reading `caller.phone_e164`:
    # `caller` is a detached row that was loaded before this request, so the guard
    # has to be evaluated by the database against the row as it is now.
    try:
        promoted = (await session.execute(
            update(User)
            .where(User.id == caller.id, User.phone_e164.is_(None))
            .values(phone_e164=phone)
            .returning(User.id))).scalar_one_or_none()
    except IntegrityError:
        # Two devices verifying the same number at once. The loser re-reads rather
        # than failing: both callers proved they hold the phone.
        await session.rollback()
        return (await session.execute(
            select(User).where(User.phone_e164 == phone))).scalars().one()

    if promoted is not None:
        logger.info(f"[auth] promoted anonymous row {caller.id} to a phone "
                    f"identity; turns already used this window carry over")
        return (await session.execute(
            select(User).where(User.id == caller.id))).scalars().one()

    # The guard failed: `caller` is already signed in as a *different* number and
    # is now verifying a new one. A row per number is the only coherent answer —
    # the quota is per identity, and these are two identities.
    user = User(phone_e164=phone, last_seen_at=datetime.now(timezone.utc))
    session.add(user)
    await session.flush()
    return user


# ── 3. sessions ─────────────────────────────────────────
async def _issue_session(res: Resources, user: User) -> Session:
    """Mint an access token and open a new refresh family for it."""
    return await _issue(res, user, family=tokens.new_family())


async def _issue(res: Resources, user: User, *, family: str) -> Session:
    access, ttl = tokens.mint_access(user.id)
    plaintext, digest = tokens.mint_refresh()
    now = datetime.now(timezone.utc)

    async with res.session_factory() as session:
        session.add(RefreshToken(
            user_id=user.id,
            token_hash=digest,
            family_id=family,
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
            created_at=now,
        ))
        await session.commit()

    return Session(user=user, access_token=access, expires_in=ttl,
                   refresh_token=plaintext)


async def rotate(res: Resources, plaintext: str | None) -> Session:
    """Exchange a refresh token for a new pair. Raises on anything suspicious.

    The reuse check is the point of this function existing rather than the client
    simply holding a long-lived token: a refresh token that has already been spent
    can only be presented a second time by a *copy* of it, so that presentation
    revokes the entire family and both the thief and the victim are logged out. The
    victim gets an SMS and carries on; the thief has nothing.
    """
    if not plaintext:
        raise AuthError(401, "refresh_missing", "Not signed in.")

    digest = tokens.hash_refresh(plaintext)
    now = datetime.now(timezone.utc)

    async with res.session_factory() as session:
        row = (await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == digest)
            .with_for_update())).scalars().first()

        if row is None:
            raise AuthError(401, "refresh_unknown", "Please sign in again.")

        if row.used_at is not None:
            await _revoke_family(session, row.family_id, now)
            await session.commit()
            # Warning, not info: this is the one log line in the auth path that
            # means someone probably has a credential they should not.
            logger.warning(f"[auth] refresh token replayed for user "
                           f"{row.user_id}; family revoked")
            raise AuthError(401, "refresh_reused",
                            "Your session was ended for security. Please sign "
                            "in again.")

        if row.revoked_at is not None or row.expires_at <= now:
            raise AuthError(401, "refresh_expired", "Please sign in again.")

        row.used_at = now
        family = row.family_id
        user = (await session.execute(
            select(User).where(User.id == row.user_id))).scalars().one_or_none()
        await session.commit()

    if user is None:                                  # deleted via DELETE /api/me
        raise AuthError(401, "refresh_unknown", "Please sign in again.")

    return await _issue(res, user, family=family)


async def logout(res: Resources, plaintext: str | None) -> None:
    """End the session the refresh token belongs to, and its whole family.

    Never raises. Logging out of a session that is already gone is a success —
    the caller asked to not be signed in, and they are not signed in.

    The **family**, not just the row: the access token cannot be revoked, so a
    logout that left the rest of the chain alive would let a rotation happen again
    fifteen minutes later. This is also why logout is worth having at all.
    """
    if not plaintext:
        return

    now = datetime.now(timezone.utc)
    async with res.session_factory() as session:
        row = (await session.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == tokens.hash_refresh(plaintext)))
        ).scalars().first()
        if row is not None:
            await _revoke_family(session, row.family_id, now)
            await session.commit()


async def _revoke_family(session: AsyncSession, family: str,
                         now: datetime) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family,
               RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now))


async def user_by_id(res: Resources, user_id: int) -> User | None:
    """The row an access token names, or None if it is gone.

    None is not an error: a token can outlive its user by up to
    `access_token_ttl_minutes` after `DELETE /api/me`, and the caller of this
    (`auth/identity.py`) treats that as "anonymous", not as a failure.
    """
    async with res.session_factory() as session:
        return (await session.execute(
            select(User).where(User.id == user_id))).scalars().first()


# ── 4. erasure (DPDP Act 2023) ──────────────────────────
async def delete_account(res: Resources, user: User) -> None:
    """Erase an identity and everything that references it.

    One `DELETE`, because every dependent table declares
    `ForeignKey(..., ondelete="CASCADE")` and Postgres does the rest. That is the
    design that makes this endpoint stay correct: slice 3 adds profiles,
    conversations and messages, and they are erased by this function on the day
    they are created rather than the day someone remembers to add them here.

    Deleting the row also frees the phone number, so the same person can sign up
    again later as a genuinely new identity with no history — which is what
    erasure has to mean for it to be erasure.
    """
    async with res.session_factory() as session:
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()
    logger.info(f"[auth] account {user.id} deleted at the user's request")
