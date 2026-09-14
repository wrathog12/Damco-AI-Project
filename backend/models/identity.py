"""
Who is calling, and how much of their allowance is left.

The product rule this table exists to serve: **there is no login wall.** A
first-time caller gets a conversation immediately and only meets an OTP screen
when they want more than the free allowance. So "a user" here is not "someone who
signed up" — it is any identity we have ever counted turns against, which on
first contact means a uuid in a signed cookie and nothing else.

Two consequences shape the schema:

1. **Both identifiers are nullable, and at least one must be present.** An
   anonymous row has `anon_id` and no `phone_e164`; an authenticated row has a
   phone. The `CheckConstraint` is what stops a row that identifies nobody, which
   would be a quota bucket no request could ever be attributed to.

2. **Logging in does not merge the anonymous row into the phone row.** The
   tempting design is to carry the anonymous quota history across, but a returning
   caller on a second device already has a phone row, so login would have to merge
   two rows with two counters — and the only thing worth merging is a count of
   turns already spent. Instead the authenticated row wins outright and the
   anonymous row is simply abandoned (`auth/identity.py::resolve`). It costs one
   dead row per new device and removes an entire class of merge bug.

Quota lives on this table rather than in a table of its own. It is strictly 1:1
with an identity, it is written on **every turn**, and the read is on the hot path
of a live voice call — so a join to fetch two integers earns nothing.
"""
from datetime import datetime

from sqlalchemy import (BigInteger, CheckConstraint, DateTime, Index, Integer,
                        String)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """One identity we count turns against — anonymous or authenticated."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # ── identity ────────────────────────────────────────────────────────
    # The uuid we minted into the signed `bh_anon` cookie. Present on every row
    # that began life anonymously, which is nearly all of them; NULL only on a row
    # created directly by an OTP login (a caller who logs in on a fresh device
    # before ever speaking).
    anon_id: Mapped[str | None] = mapped_column(String(36), unique=True)

    # E.164, e.g. "+919876543210". Normalised on write — storing the same number
    # in two formats would silently hand one person two quota buckets, which is
    # the exact hole this whole table is here to close.
    #
    # NOT an Aadhaar number, and never will be: see the DPDP note below.
    phone_e164: Mapped[str | None] = mapped_column(String(16), unique=True)

    # ── consent (DPDP Act 2023) ─────────────────────────────────────────
    # Recorded per version so a policy change can require re-consent instead of
    # silently inheriting agreement to terms the user never saw. NULL means "has
    # not consented yet", which is the correct state for an anonymous caller: we
    # store no personal data about them, so there is nothing to consent to until
    # a profile write is attempted.
    consent_version: Mapped[str | None] = mapped_column(String(16))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── quota ───────────────────────────────────────────────────────────
    # Turns spent inside the current window. Reset — not decremented — when the
    # window rolls over; see `services/quota.py`.
    turns_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                            server_default="0")
    # When the current window began, which is the caller's **first turn of that
    # window**, not midnight. A calendar-day reset would hand someone who ran out
    # at 23:55 a fresh allowance five minutes later; anchoring to first use means
    # the window a caller experiences is always the full `quota_window_hours`.
    #
    # NULL means "no turn ever counted", which is distinct from a window that has
    # expired, and the two must not be conflated — the first is a new caller, the
    # second is a returning one.
    quota_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))

    # Cheap analytics that costs one column: lets an abandoned anonymous row be
    # pruned without inferring liveness from `updated_at`, which quota writes
    # touch constantly.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # A row identifying nobody is a quota bucket nothing can be attributed
        # to — it would leak allowance rather than enforce it.
        CheckConstraint("anon_id IS NOT NULL OR phone_e164 IS NOT NULL",
                        name="ck_users_has_identity"),
        # The window-rollover sweep and the prune both scan on this.
        Index("ix_users_quota_window", "quota_window_started_at"),
    )

    # Neither identifier is logged. `anon_id` is a bare uuid so it is not
    # sensitive, but a phone number is, and the one rule with no exceptions is
    # the one about Aadhaar: it is never stored, in this table or any other.
    def __repr__(self) -> str:                                  # pragma: no cover
        who = "anon" if self.phone_e164 is None else "user"
        return f"<User id={self.id} {who} turns_used={self.turns_used}>"
