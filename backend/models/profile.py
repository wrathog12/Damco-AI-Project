"""
What we remember about a caller, and the rules that shape the schema.

Slice 1 gave every caller an identity and a turn counter. Slice 2 let them prove a
phone number. This is the slice that makes the agent stop asking "which state are
you from?" on every call — and it is the first slice that stores anything a
citizen would mind us losing, so the schema is driven as much by the DPDP Act 2023
as by the feature.

## Four rules, visible in the columns

1. **Nothing personal is stored for an anonymous caller.** Every table here hangs
   off `users.id`, but a row is only written for an identity with a phone number.
   An anonymous caller cannot exercise erasure — `DELETE /api/me` needs a verified
   token by design — so recording their income against a cookie would create
   personal data its subject has no way to reach. They get the whole product,
   minus the memory.

2. **The sensitive values are encrypted by the application** (`auth/crypto.py`),
   not merely by the disk. Hence `LargeBinary` where a reader would expect
   `Integer` and `String`: `age`, `income`, `caste` and `disability` are what the
   Act calls sensitive, and the profile is only ever read whole by primary key, so
   encryption costs no query this system performs.

3. **Consent is recorded where the data is, not only on the user.**
   `user_profiles.consent_version` says which policy the facts in that row were
   collected under. A policy change can then require fresh consent instead of
   silently inheriting agreement to terms the caller never saw — which is what
   `users.consent_version` alone would allow.

4. **Retention is per-table and enforced by a sweep** (`services/retention.py`),
   because "we delete old data" is a promise and a scheduled `DELETE` is a fact.
   Transcripts expire first and fastest; a record of which schemes someone looked
   at lives longer, because that is what makes the second call better than the
   first.

## Why some references are foreign keys and some are plain strings

`user_id` is always a real FK with `ondelete="CASCADE"`, and that is what keeps
`DELETE /api/me` a single statement — the erasure endpoint stays correct because
the database enforces it, not because someone remembered to extend a function.

`scheme_id` is deliberately **not** a foreign key. Ingestion rebuilds the corpus
and a scheme can be delisted or renamed; an interaction is a record of what a
person did, and it must survive the catalogue changing under it. A `CASCADE` there
would quietly rewrite history and a `RESTRICT` would break ingestion.

`scheme_interactions.conversation_id` is a plain string for a related reason:
conversations are purged after `conversation_retention_days` while interactions
live for `interaction_retention_days`, so a cascading FK would drag the longer
retention down to the shorter one.
"""
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Index,
                        Integer, LargeBinary, SmallInteger, String,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class UserProfile(Base, TimestampMixin):
    """The eligibility inputs for one caller — the point of the whole slice.

    One row per user, keyed on `user_id` rather than an id of its own: it is
    strictly 1:1, it is always fetched by that key, and a surrogate key would only
    create the possibility of two profiles for one person.

    The facts themselves are a single encrypted JSON object rather than a column
    each. Per-column ciphertext would buy the ability to update one field without
    reading the others, which nothing wants, and cost a nonce per column plus the
    loss of every value's type. One blob keeps the shape of the data in Python
    where the validation already lives.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)

    # AES-256-GCM over a JSON object of the known fields; see auth/crypto.py.
    # Bound to `profile:<user_id>` so a row copied between users fails to open.
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Bumped on every write. `eligibility_evaluations` caches a verdict against
    # this number, so a profile edit invalidates exactly the cached answers that
    # were computed from the old facts — without a timestamp comparison that has
    # to reason about clock skew.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                         server_default="1")

    # Which consent version was in force when these facts were collected. See
    # rule 3 above. Not nullable: a profile row without it would be data whose
    # basis for being held is unknown.
    consent_version: Mapped[str] = mapped_column(String(16), nullable=False)

    def __repr__(self) -> str:                                  # pragma: no cover
        # Never the payload, and never anything derived from it — a repr ends up
        # in logs and exception reports, which is exactly where income and caste
        # must not be.
        return f"<UserProfile user={self.user_id} v{self.version}>"


class Conversation(Base):
    """One call or one chat thread.

    `id` is a string because it is the uuid `POST /api/offer` already mints and
    hands to the pipeline as `conversation_id` — reusing it means a row can be
    written at connect time and finished at hang-up without a second identifier to
    correlate.

    A row with `ended_at IS NULL` is either live or was interrupted by a process
    that died. That ambiguity is deliberate: inventing an end time we never
    observed would make session-length statistics quietly false.
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # "voice" | "text". Both spend the same turn allowance, and both are worth
    # persisting: the text path is the accessibility path (see the plan's P4 notes
    # on a browse mode), not a debug tool.
    channel: Mapped[str] = mapped_column(String(8), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "end_call", "inactivity", "disconnect" — the same vocabulary the pipeline
    # already uses, so a call that ended badly is distinguishable from one the
    # caller finished.
    end_reason: Mapped[str | None] = mapped_column(String(24))

    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                            server_default="0")
    # Deepgram's report for the last utterance of the call. Stored so the recap
    # injected into the *next* session can be written in the right language
    # without waiting for the caller to speak first.
    language: Mapped[str | None] = mapped_column(String(8))

    __table_args__ = (
        # The recap query: this user's conversations, most recent first. The
        # retention sweep scans `started_at` alone and is allowed to seq-scan —
        # it runs once a day over a table this app writes once per call, and an
        # index for it would be paid for on every write instead.
        Index("ix_conversations_user_started", "user_id", "started_at"),
    )

    def __repr__(self) -> str:                                  # pragma: no cover
        return (f"<Conversation {self.id[:8]} user={self.user_id} "
                f"{self.channel} turns={self.turn_count}>")


class Message(Base):
    """One turn of one conversation, encrypted.

    The transcript is the most sensitive thing this system holds and the least
    structured: a caller explaining why they need help says more about themselves
    in one sentence than the whole profile records. So `body` is sealed the same
    way the profile is, and the retention window on this table is the shortest one
    in the schema.

    `seq` rather than ordering by `created_at`: two messages of one turn can share
    a timestamp to the microsecond, and a transcript that renders out of order
    reads as the agent answering before it was asked.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    # "user" | "assistant". No "system": the language notices and quota
    # instructions the pipeline injects are scaffolding, not conversation, and
    # storing them would put our own prompt text into the caller's transcript.
    role: Mapped[str] = mapped_column(String(12), nullable=False)
    # Sealed, bound to `message:<conversation_id>`.
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)

    __table_args__ = (
        # Doubles as the index for "the messages of this conversation, in order",
        # so there is no separate index on `conversation_id` — the composite's
        # leading column already covers it.
        UniqueConstraint("conversation_id", "seq", name="uq_messages_seq"),
    )

    def __repr__(self) -> str:                                  # pragma: no cover
        return (f"<Message {self.conversation_id[:8]}#{self.seq} "
                f"{self.role} {len(self.body or b'')}B>")


class SchemeInteraction(Base):
    """What the caller actually did with a scheme.

    This is the cheapest useful memory in the system: it needs no consent screen
    beyond the one already given, it holds no sensitive category, and it is what
    lets the next call open with "last time we looked at Kanyashree" instead of
    starting from nothing.

    Append-only. An interaction is an event, and events are not edited.
    """

    __tablename__ = "scheme_interactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Plain string, not an FK — see the module docstring.
    scheme_id: Mapped[str] = mapped_column(String(160), nullable=False)
    # "viewed" | "card_shown" | "eligibility_checked" | "apply_clicked"
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # Which call it happened on, when it happened on one. Nullable and unenforced:
    # an interaction can outlive the conversation it came from.
    conversation_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)

    __table_args__ = (
        Index("ix_interactions_user_created", "user_id", "created_at"),
    )

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"<SchemeInteraction user={self.user_id} {self.kind} {self.scheme_id}>"


class EligibilityEvaluation(Base):
    """A cached verdict for one (user, scheme) pair.

    A cache, not a record: the authority is always
    `services/schemes.py::evaluate_eligibility` run against the current profile,
    and a row here is only trusted while `profile_version` still matches. That is
    what makes "you are eligible for 12 schemes you have not looked at" a cheap
    query instead of 1,786 rule evaluations per page load, without ever showing a
    verdict computed from facts the caller has since corrected.

    `eligible` is in the clear because the count is the whole point of the table
    and a boolean per scheme reveals nothing on its own. `reasons` is sealed
    because the strings quote the inputs — "Income ₹48,000: ✓ within limit" is the
    caller's income written out in prose.
    """

    __tablename__ = "eligibility_evaluations"

    # Composite primary key: the pair *is* the identity of the row, and it gives
    # the lookup index for free. A surrogate id would allow two cached verdicts
    # for the same pair, which is the one state this table must not reach.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    scheme_id: Mapped[str] = mapped_column(String(160), primary_key=True)

    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reasons: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # The `user_profiles.version` these reasons were computed from.
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   nullable=False)

    def __repr__(self) -> str:                                  # pragma: no cover
        return (f"<EligibilityEvaluation user={self.user_id} "
                f"{self.scheme_id} eligible={self.eligible}>")
