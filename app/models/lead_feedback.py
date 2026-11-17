"""The `LeadFeedback` table: a person's verdict on a delivered lead. The start of a real accuracy dataset."""

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedMixin

VERDICTS = ("good", "wrong_company", "wrong_intent", "wrong_contact")


class LeadFeedback(TimestampMixin, UpdatedMixin, Base):
    """One verdict per lead (changing your mind replaces it). Kept with the lead so every verdict can be
    traced to the evidence, score and plan that produced it."""

    __tablename__ = "lead_feedback"
    __table_args__ = (CheckConstraint(f"verdict IN {VERDICTS}", name="verdict_valid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_lead_id: Mapped[int] = mapped_column(ForeignKey("job_leads.id", ondelete="CASCADE"), unique=True)
    verdict: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
