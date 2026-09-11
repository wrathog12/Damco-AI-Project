"""
Canonical scheme tables.

Postgres is the source of truth; Qdrant is a derived index rebuilt from these
rows. Two design rules carried over from the v1 knowledge base:

1. **A NULL eligibility column means "unspecified", and must never exclude a
   user.** Every filter has to skip its own check when the scheme's value is
   NULL. This is why `age_min`, `income_max`, `caste` etc. are nullable rather
   than defaulted — a default would quietly turn "not stated" into a constraint.

2. **Filters are hard constraints, semantic search only ranks within them.** So
   the fields a filter touches are typed columns with indexes, not JSONB keys.
"""
from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Index, Integer,
                        Numeric, String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin


class Scheme(Base, TimestampMixin):
    """One government welfare scheme, English canonical."""

    __tablename__ = "schemes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── identity ────────────────────────────────────────────────────────
    # The myScheme slug. Stable, unique, and the key the detail endpoint takes,
    # so it doubles as our public scheme_id in tool calls.
    scheme_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    # myScheme's internal ObjectId — needed to re-fetch sub-resources
    # (documents/faqs), which are keyed on it rather than on the slug.
    myscheme_id: Mapped[str | None] = mapped_column(String(48))

    scheme_name: Mapped[str] = mapped_column(Text, nullable=False)
    scheme_short_title: Mapped[str | None] = mapped_column(String(120))

    # ── classification (filterable) ─────────────────────────────────────
    # `state` is the primary one for display; `states` is authoritative for
    # filtering, because a scheme can cover several states or all of them.
    state: Mapped[str | None] = mapped_column(String(80), index=True)
    states: Mapped[list[str] | None] = mapped_column(ARRAY(String(80)))
    level: Mapped[str | None] = mapped_column(String(20), index=True)  # Central|State
    category: Mapped[str | None] = mapped_column(String(120), index=True)
    categories: Mapped[list[str] | None] = mapped_column(ARRAY(String(120)))
    subcategories: Mapped[list[str] | None] = mapped_column(ARRAY(String(120)))
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(80)))

    nodal_ministry: Mapped[str | None] = mapped_column(Text)
    nodal_department: Mapped[str | None] = mapped_column(Text)
    implementing_agency: Mapped[str | None] = mapped_column(Text)
    scheme_for: Mapped[str | None] = mapped_column(String(60))
    target_beneficiaries: Mapped[list[str] | None] = mapped_column(ARRAY(String(80)))
    dbt_scheme: Mapped[bool | None] = mapped_column(Boolean)

    # ── content ─────────────────────────────────────────────────────────
    description: Mapped[str | None] = mapped_column(Text)          # brief
    detailed_description: Mapped[str | None] = mapped_column(Text)  # markdown
    benefits_md: Mapped[str | None] = mapped_column(Text)
    exclusions_md: Mapped[str | None] = mapped_column(Text)
    eligibility_description: Mapped[str | None] = mapped_column(Text)

    objectives: Mapped[list | None] = mapped_column(JSONB)
    benefits: Mapped[list | None] = mapped_column(JSONB)
    benefit_types: Mapped[list[str] | None] = mapped_column(ARRAY(String(60)))
    documents_required: Mapped[list | None] = mapped_column(JSONB)
    application_process: Mapped[list | None] = mapped_column(JSONB)
    scheme_definitions: Mapped[list | None] = mapped_column(JSONB)
    references: Mapped[list | None] = mapped_column(JSONB)

    application_url: Mapped[str | None] = mapped_column(Text)
    application_modes: Mapped[list[str] | None] = mapped_column(ARRAY(String(40)))
    helpline: Mapped[str | None] = mapped_column(Text)

    # ── eligibility, typed. NULL == unspecified == do not exclude ───────
    age_min: Mapped[int | None] = mapped_column(Integer)
    age_max: Mapped[int | None] = mapped_column(Integer)
    income_max: Mapped[float | None] = mapped_column(Numeric(14, 2))
    gender: Mapped[list[str] | None] = mapped_column(ARRAY(String(20)))
    caste: Mapped[list[str] | None] = mapped_column(ARRAY(String(40)))
    occupation: Mapped[str | None] = mapped_column(String(120))
    disability: Mapped[bool | None] = mapped_column(Boolean)
    bpl_card: Mapped[bool | None] = mapped_column(Boolean)
    state_residence: Mapped[list[str] | None] = mapped_column(ARRAY(String(80)))
    minority: Mapped[bool | None] = mapped_column(Boolean)
    student: Mapped[bool | None] = mapped_column(Boolean)
    marital_status: Mapped[list[str] | None] = mapped_column(ARRAY(String(30)))

    # ── provenance / change detection ───────────────────────────────────
    scheme_open_date: Mapped[date | None] = mapped_column(Date)
    scheme_close_date: Mapped[date | None] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(Text)
    # sha256 of the canonical English content only, so a re-fetch with new
    # timestamps or added translations does not look like a content change.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   nullable=False)
    # Set when the scheme stops appearing in the index — de-listing detection,
    # which v1 could not do at all (its knowledge base only ever grew).
    delisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    translations: Mapped[list["SchemeTranslation"]] = relationship(
        back_populates="scheme", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_schemes_states_gin", states, postgresql_using="gin"),
        Index("ix_schemes_tags_gin", tags, postgresql_using="gin"),
        Index("ix_schemes_categories_gin", categories, postgresql_using="gin"),
        Index("ix_schemes_state_category", "state", "category"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Scheme {self.scheme_id} {self.scheme_name[:40]!r}>"


class SchemeTranslation(Base):
    """A scheme rendered in one language.

    Unlike v1 these are *fetched*, not machine-translated — myScheme serves
    native hi/bn/mr/ta. `source_content_hash` records which English revision the
    translation corresponds to, so a changed scheme can be re-fetched rather than
    silently serving stale text (v1's `--resume` skipped any scheme whose
    translation merely existed, so it could never refresh one).
    """

    __tablename__ = "scheme_translations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheme_pk: Mapped[int] = mapped_column(
        ForeignKey("schemes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lang: Mapped[str] = mapped_column(String(8), nullable=False)

    scheme_name: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    detailed_description: Mapped[str | None] = mapped_column(Text)
    benefits_md: Mapped[str | None] = mapped_column(Text)
    eligibility_description: Mapped[str | None] = mapped_column(Text)
    documents_required: Mapped[list | None] = mapped_column(JSONB)
    payload: Mapped[dict | None] = mapped_column(JSONB)   # full block, verbatim

    # TRANSLATED | EN_FALLBACK | EMPTY | MISSING — see ingestion.harvest
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="TRANSLATED")
    source_content_hash: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    scheme: Mapped[Scheme] = relationship(back_populates="translations")

    __table_args__ = (
        UniqueConstraint("scheme_pk", "lang", name="uq_scheme_translation_lang"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SchemeTranslation {self.scheme_pk}/{self.lang} {self.status}>"


class SchemeEmbeddingState(Base):
    """Which content revision of a scheme is currently in Qdrant.

    Pass B (embed -> Qdrant) diffs against this so re-indexing is incremental
    and never has to re-hit the API.
    """

    __tablename__ = "scheme_embedding_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheme_pk: Mapped[int] = mapped_column(
        ForeignKey("schemes.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("scheme_pk", "model", name="uq_scheme_embedding_model"),
    )
