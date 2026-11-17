"""The `JobLead` table."""

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, str_enum
from app.models.enums import (
    LeadStatus,
    LeadTier,
)


class JobLead(TimestampMixin, Base):
    __tablename__ = "job_leads"
    __table_args__ = (
        UniqueConstraint("job_id", "company_id"),
        CheckConstraint("fit_score BETWEEN 0 AND 40", name="fit_range"),
        CheckConstraint("intent_score BETWEEN 0 AND 40", name="intent_range"),
        CheckConstraint("timing_score BETWEEN 0 AND 20", name="timing_range"),
        CheckConstraint(
            "total_score IS NULL OR total_score = fit_score + intent_score + timing_score",
            name="total_is_sum",
        ),
        CheckConstraint("status = 'rejected' OR total_score IS NOT NULL", name="non_rejected_has_score"),
        CheckConstraint("status <> 'rejected' OR reject_reason IS NOT NULL", name="rejected_has_reason"),
        CheckConstraint("status = 'rejected' OR lead_tier IS NOT NULL", name="non_rejected_has_tier"),
        Index("ix_job_leads_job_status", "job_id", "status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    fit_score: Mapped[int | None] = mapped_column(Integer)
    intent_score: Mapped[int | None] = mapped_column(Integer)
    timing_score: Mapped[int | None] = mapped_column(Integer)
    total_score: Mapped[int | None] = mapped_column(Integer)
    why_summary: Mapped[str | None] = mapped_column(Text)
    outreach_angle: Mapped[str | None] = mapped_column(Text)
    risks: Mapped[str | None] = mapped_column(Text)
    alive_proofs: Mapped[list] = mapped_column(JSON, default=list)
    outreach_allowed: Mapped[bool | None] = mapped_column(Boolean)
    outreach_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[LeadStatus] = mapped_column(str_enum(LeadStatus), default=LeadStatus.CANDIDATE)
    lead_tier: Mapped[LeadTier | None] = mapped_column(str_enum(LeadTier))
    # Combined buying signs (signal_intelligence_service.combine): strong | good | weak, and a one-line summary.
    confidence_tier: Mapped[str | None] = mapped_column(String(10))
    signal_summary: Mapped[str | None] = mapped_column(Text)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    rank: Mapped[int | None] = mapped_column(Integer)
    # [{contact_id, subject, body, evidence_ids}] when personalized_email is on
    email_drafts: Mapped[list] = mapped_column(JSON, default=list)
