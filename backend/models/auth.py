"""
The two tables login needs: an outstanding OTP, and a live refresh token.

Both hold something sensitive, and the shape of each is driven by what happens
when the database leaks rather than by what is convenient to query.

## Neither table stores the secret it checks

`OtpChallenge` keeps an HMAC of the code, not the code. `RefreshToken` keeps a
hash of the token, not the token. In both cases the plaintext exists exactly
once — in the SMS, or in the caller's cookie — so a dump of these tables lets
nobody log in as anybody. That costs nothing: verification is a hash-and-compare
either way.

## OTP rows are deleted, not marked consumed

There is no `consumed_at`. A successful login **deletes every challenge for that
phone**, which invalidates any other outstanding code in the same statement and,
more importantly, leaves no lingering record of who tried to log in and when.
Expired rows are swept on the next send.

That is a DPDP decision, not tidiness: a phone number is personal data, the
purpose it was collected for ends the moment the code is verified or expires, and
purpose limitation means the row goes with it. `users.phone_e164` is the one place
a number is *kept*, because that is the identity itself.

## Refresh tokens are a family, so theft is detectable

Rotation alone does not help if an attacker steals a refresh token and uses it
before the real caller does — both sides just keep rotating. What catches it is
`used_at` plus `family_id`: presenting a token that has already been spent is
something only a *copy* can do, so that presentation revokes the whole family and
both parties are logged out. The real user re-authenticates with an SMS; the
attacker cannot.
"""
from datetime import datetime

from sqlalchemy import (BigInteger, DateTime, ForeignKey, Index, Integer,
                        SmallInteger, String)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class OtpChallenge(Base):
    """One code sent to one phone, awaiting verification."""

    __tablename__ = "otp_challenges"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Not unique: a resend is a new row, and the newest unexpired one is what
    # `verify` checks. A unique constraint would force an UPDATE, which would
    # silently make the second send overwrite a code the caller may already be
    # typing from an SMS that arrived first.
    phone_e164: Mapped[str] = mapped_column(String(16), nullable=False)

    # HMAC of the code under `jwt_secret`, not the code and not a bare digest: a
    # six-digit number has a million possibilities, so an unkeyed hash of it is
    # reversible by brute force in milliseconds.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    # Counted per challenge, so a wrong code cannot be brute-forced by retrying
    # against the same row. `otp_max_attempts` is the cap; hitting it burns the
    # challenge and the caller has to request a new code.
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0,
                                          server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)

    __table_args__ = (
        # Every read is "the newest live challenge for this phone", and every
        # rate-limit check is "how many for this phone since when".
        Index("ix_otp_phone_created", "phone_e164", "created_at"),
        # The retention sweep scans this.
        Index("ix_otp_expires", "expires_at"),
    )

    def __repr__(self) -> str:                                  # pragma: no cover
        # No phone number, ever — this is the object most likely to end up in a
        # log line while someone is debugging a failing login.
        return f"<OtpChallenge id={self.id} attempts={self.attempts}>"


class RefreshToken(Base):
    """One long-lived credential, hashed, rotated on use."""

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # CASCADE is what makes `DELETE /api/me` actually delete: a user row that
    # disappears must not leave a credential that still names it.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # SHA-256 of the opaque token. Unique because the lookup is by hash and two
    # rows with the same hash would make "which session is this?" ambiguous.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False,
                                            unique=True)
    # Every token minted by rotating another shares its family. One stolen token
    # therefore takes down exactly one login chain, not every device. Indexed by
    # the composite in `__table_args__`, whose leading column this is — a second
    # single-column index on it would be dead weight on every insert.
    family_id: Mapped[str] = mapped_column(String(36), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    # Stamped when this token is exchanged. A second presentation of a stamped
    # token is a replay, and replay revokes the family — see the module docstring.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                nullable=False)

    __table_args__ = (
        # Logout and reuse-detection both revoke by family.
        Index("ix_refresh_family_revoked", "family_id", "revoked_at"),
    )

    @property
    def spendable(self) -> bool:
        """Unrevoked and unspent. Expiry is checked against the DB clock in
        `services/auth.py`, not here — an ORM object read minutes ago should not
        be the thing that decides whether a credential is still live."""
        return self.used_at is None and self.revoked_at is None

    def __repr__(self) -> str:                                  # pragma: no cover
        state = "live" if self.spendable else "spent"
        return f"<RefreshToken id={self.id} user={self.user_id} {state}>"
