"""
Persisting a conversation, and the behavioural memory built from it.

Two things live here: the transcript (tier L1 of the plan's memory model) and the
record of which schemes a caller actually engaged with (tier L3). They are stored
by the same code because they come from the same events, and they are governed by
different retention windows because they are not equally revealing.

## The transcript is persisted and deliberately not replayed

`services/profiles.py::session_context` builds a *recap* for the next session, not
a restored message list. Handing a week-old `messages` array back to the model
makes it resume a thread the caller has forgotten, and it puts hundreds of tokens
in front of every turn of the new call for the privilege. So the transcript exists
for the caller's own benefit — a history view in P4, and their right to a copy of
what we hold — while the thing that actually improves the second call is a short
recap of facts and scheme names.

That is also why `messages` never stores a `system` row. The language notices and
quota instructions the pipeline injects are our scaffolding; putting them in the
caller's transcript would make our prompt text part of their personal data, and
would render as the agent talking to itself in any history view.

## Single writer per conversation

`append` derives `seq` from `max(seq) + 1`, which is only safe because exactly one
task writes any given conversation: the pipeline's own transcript recorder for a
voice call, the request handler for a text turn. The unique constraint on
`(conversation_id, seq)` is the backstop that turns a violation of that assumption
into an error instead of a silently reordered transcript.

## Nothing is written for an anonymous caller

Same rule as the profile, same reason: `DELETE /api/me` requires a verified token,
so a transcript recorded against a cookie would be personal data its subject
cannot reach. `persists_for` is the one check, and every caller asks it first
rather than each deciding for itself.
"""
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from loguru import logger
from sqlalchemy import delete, func, insert, select, update

from auth import crypto
from config import settings
from models.identity import User
from models.profile import Conversation, Message, SchemeInteraction
from models.scheme import Scheme
from services.resources import Resources

# The vocabulary `record_interaction` accepts. A closed set because these strings
# are what a future "schemes you looked at but never applied for" query filters
# on, and a typo would silently create a category nothing reads.
KINDS = ("viewed", "card_shown", "eligibility_checked", "apply_clicked")

# Roles a stored message may have. See the module docstring on why "system" is
# absent.
ROLES = ("user", "assistant")


def _aad(conversation_id: str) -> str:
    """Message bodies are bound to their conversation, not to each row.

    Per-row binding would need the message id, which does not exist until the
    insert has happened — and the property worth having is that a transcript
    cannot be moved between conversations, which this gives.
    """
    return f"message:{conversation_id}"


def persists_for(user: User) -> bool:
    """Whether this caller's conversation should be recorded at all."""
    return bool(user.phone_e164) and crypto.available()


async def start(res: Resources, user: User, *, conversation_id: str,
                channel: str) -> bool:
    """Open a conversation row. Returns whether one was written.

    Idempotent on the id, because both `/chat` and the voice path may open the
    same conversation more than once — a text client sends the same
    `conversation_id` on every turn of a thread, and re-opening must not lose the
    turns already recorded against it.
    """
    if not persists_for(user):
        return False

    now = datetime.now(timezone.utc)
    async with res.session_factory() as session:
        existing = (await session.execute(
            select(Conversation.id)
            .where(Conversation.id == conversation_id))).scalar_one_or_none()
        if existing is not None:
            return True
        session.add(Conversation(id=conversation_id, user_id=user.id,
                                 channel=channel, started_at=now, turn_count=0))
        await session.commit()
    logger.debug(f"[memory] conversation {conversation_id[:8]} opened "
                 f"({channel}) for user {user.id}")
    return True


async def append(res: Resources, conversation_id: str,
                 turns: Iterable[tuple[str, str]]) -> int:
    """Append `(role, text)` pairs to a conversation. Returns how many landed.

    Empty and whitespace-only bodies are dropped rather than stored: a TTS turn
    that produced no text is not a thing the caller said, and a blank row in a
    transcript reads as a pause that never happened.
    """
    if not crypto.available():
        return 0

    rows: list[tuple[str, str]] = []
    for role, text in turns:
        if role not in ROLES:
            logger.warning(f"[memory] refusing to store a {role!r} message")
            continue
        body = (text or "").strip()
        if body:
            rows.append((role, body))
    if not rows:
        return 0

    now = datetime.now(timezone.utc)
    async with res.session_factory() as session:
        # The conversation may have been purged by the retention sweep between a
        # long call starting and this write; the FK would then fail. Checking
        # first turns that into "nothing to record" instead of an exception on a
        # path whose failure the caller cannot act on.
        known = (await session.execute(
            select(Conversation.id)
            .where(Conversation.id == conversation_id))).scalar_one_or_none()
        if known is None:
            return 0

        next_seq = (await session.execute(
            select(func.coalesce(func.max(Message.seq), 0))
            .where(Message.conversation_id == conversation_id))).scalar_one() + 1

        session.add_all([
            Message(conversation_id=conversation_id, seq=next_seq + offset,
                    role=role,
                    body=crypto.encrypt(text.encode(),
                                        aad=_aad(conversation_id)),
                    created_at=now)
            for offset, (role, text) in enumerate(rows)
        ])

        # A turn is a thing the *user* said, which is also the quota's unit. Kept
        # here rather than left to `finish` because a text thread has no hang-up:
        # `/chat` calls `finish` on every turn and cannot know the running total.
        # A caller that does know — the voice path, from its recorder — passes an
        # authoritative count to `finish`, which overwrites this rather than
        # adding to it, so the two never double-count.
        spoken = sum(1 for role, _ in rows if role == "user")
        if spoken:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(turn_count=Conversation.turn_count + spoken))

        await session.commit()
    return len(rows)


async def finish(res: Resources, conversation_id: str, *, reason: str,
                 turn_count: int | None = None,
                 language: str | None = None) -> None:
    """Stamp the end of a conversation. Safe to call for a row that never existed.

    `ended_at` is only ever set from an end we actually observed — the sweep does
    not backfill it for a call the process died during, because inventing an end
    time would make session-length statistics quietly false.
    """
    if not crypto.available():
        return

    values: dict[str, object] = {
        "ended_at": datetime.now(timezone.utc),
        "end_reason": reason[:24],
    }
    if turn_count is not None:
        values["turn_count"] = turn_count
    if language:
        values["language"] = language[:8]

    async with res.session_factory() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id).values(**values))
        await session.commit()


async def record_interaction(res: Resources, user: User, *, scheme_id: str,
                             kind: str,
                             conversation_id: str | None = None) -> bool:
    """Note that this caller did something with this scheme.

    Deliberately cheaper to store than a transcript and kept for longer: it holds
    no sensitive category, so it is not encrypted, and it is the thing that lets
    the next call open with "last time we looked at Kanyashree".

    Failure is swallowed. This is called from tool handlers, and a tool that
    returned an error because a *statistic* could not be written would break an
    answer the caller was waiting for.
    """
    if not persists_for(user) or kind not in KINDS or not scheme_id:
        return False
    try:
        async with res.session_factory() as session:
            await session.execute(insert(SchemeInteraction).values(
                user_id=user.id, scheme_id=scheme_id[:160], kind=kind,
                # Truncated like `scheme_id`, and for a sharper reason: this
                # column is not a foreign key (see models/profile.py), so an
                # over-long id is not caught by a constraint — it is a
                # `value too long for character varying(36)` that this function
                # then swallows, losing the interaction with only a log line.
                conversation_id=conversation_id[:36] if conversation_id else None,
                created_at=datetime.now(timezone.utc)))
            await session.commit()
        return True
    except Exception as exc:                                    # noqa: BLE001
        logger.warning(f"[memory] could not record {kind} on {scheme_id}: "
                       f"{type(exc).__name__}: {exc}")
        return False


async def recent_schemes(res: Resources, user: User, *,
                         limit: int = 3,
                         within_days: int = 30) -> list[str]:
    """Names of the schemes this caller last engaged with, newest first.

    Names, not ids: the consumer is the recap prompt, and a model handed
    `pm-vishwakarma` will read the slug out loud. The join also filters to
    schemes that still exist, so a recap never mentions something the corpus has
    since dropped — which is the whole reason `scheme_id` is not a foreign key
    and the reason this query has to tolerate a miss.

    `within_days` keeps the recap relevant. Recognising a caller from last week
    is welcome; opening with a scheme they looked at eleven months ago is not.
    """
    if not user.phone_e164:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=within_days)
    async with res.session_factory() as session:
        rows = (await session.execute(
            select(Scheme.scheme_name,
                   func.max(SchemeInteraction.created_at).label("last_seen"))
            .join(Scheme, Scheme.scheme_id == SchemeInteraction.scheme_id)
            .where(SchemeInteraction.user_id == user.id,
                   SchemeInteraction.created_at >= cutoff,
                   Scheme.delisted_at.is_(None))
            # Grouped by name so three interactions with one scheme do not fill
            # the whole recap with the same title.
            .group_by(Scheme.scheme_name)
            .order_by(func.max(SchemeInteraction.created_at).desc())
            .limit(limit))).all()
    return [name for name, _ in rows]


async def history(res: Resources, conversation_id: str) -> list[dict[str, object]]:
    """A decrypted transcript, in order. For the caller's own data, nothing else.

    No endpoint exposes this yet — the history view is a P4 surface — but it is
    written here because the DPDP right of access is not optional and a
    transcript that can only be read by decrypting blobs by hand is a right in
    name only. A row that will not decrypt is skipped rather than shown as an
    error, matching how the profile behaves.
    """
    async with res.session_factory() as session:
        rows = (await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq))).scalars().all()

    out: list[dict[str, object]] = []
    for row in rows:
        text = crypto.decrypt(row.body, aad=_aad(conversation_id))
        if text is None:
            continue
        out.append({"seq": row.seq, "role": row.role,
                    "text": text.decode(errors="replace"),
                    "at": row.created_at})
    return out


async def erase_all(res: Resources, user: User) -> dict[str, int]:
    """Drop this caller's transcripts and interactions, keeping the account.

    The companion to `profiles.erase`: someone who wants the agent to forget what
    they said should not have to close their account to get it. `DELETE /api/me`
    still does everything at once through the cascades.
    """
    async with res.session_factory() as session:
        # Messages go with the conversations by cascade, so one DELETE covers
        # both — the same property that keeps the erasure endpoint a single
        # statement.
        convs = await session.execute(
            delete(Conversation).where(Conversation.user_id == user.id))
        interactions = await session.execute(
            delete(SchemeInteraction).where(SchemeInteraction.user_id == user.id))
        await session.commit()
    counts = {"conversations": convs.rowcount or 0,
              "interactions": interactions.rowcount or 0}
    logger.info(f"[privacy] history erased for user {user.id}: {counts}")
    return counts


def turn_pairs(user_text: str | None,
               assistant_text: str | None) -> Sequence[tuple[str, str]]:
    """One turn as `append` wants it. Trivial, and it keeps the ordering in one
    place — a transcript with the answer before the question is worse than none."""
    pairs: list[tuple[str, str]] = []
    if user_text:
        pairs.append(("user", user_text))
    if assistant_text:
        pairs.append(("assistant", assistant_text))
    return pairs


def retention_window_days() -> int:
    """What the privacy notice has to say about transcripts."""
    return settings.conversation_retention_days
