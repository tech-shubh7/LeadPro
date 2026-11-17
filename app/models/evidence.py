"""The `Evidence` table."""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, str_enum
from app.models.enums import (
    DatePrecision,
    DateSource,
    EvidenceKind,
)


class Evidence(TimestampMixin, Base):
    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint("quote_verified", name="only_verified_quotes"),  # unverified claims are never stored
        CheckConstraint("event_date IS NULL OR event_date_quote IS NOT NULL", name="event_date_has_quote"),
        Index("ix_evidence_company_kind", "company_id", "kind"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[EvidenceKind] = mapped_column(str_enum(EvidenceKind))
    signal_type: Mapped[str | None] = mapped_column(String(80))
    claim: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(String(2000))
    quote: Mapped[str] = mapped_column(Text)
    evidence_date: Mapped[date | None] = mapped_column(
        Date
    )  # when the source was published (trusted sources only)
    event_date: Mapped[date | None] = mapped_column(Date)  # when the event happened, if the source states it
    event_date_quote: Mapped[str | None] = mapped_column(
        String(200)
    )  # the verbatim date phrase it was parsed from
    event_key: Mapped[str | None] = mapped_column(String(120))  # same key = same real-world event
    corroborating_urls: Mapped[list] = mapped_column(
        JSON, default=list, server_default=text("'[]'")
    )  # other sources reporting the same event
    date_source: Mapped[DateSource | None] = mapped_column(str_enum(DateSource))
    # How far to trust this signal, and whether it shows buying intent (signal_intelligence_service).
    intent: Mapped[str | None] = mapped_column(String(40))
    intent_reason: Mapped[str | None] = mapped_column(Text)
    source_class: Mapped[str | None] = mapped_column(String(30))
    date_confidence: Mapped[str | None] = mapped_column(String(10))
    # The snapshot this quote was verified against, and where in it the quote starts (normalised text).
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    quote_offset: Mapped[int | None] = mapped_column(Integer)
    date_precision: Mapped[DatePrecision | None] = mapped_column(str_enum(DatePrecision))
    quote_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    revalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
