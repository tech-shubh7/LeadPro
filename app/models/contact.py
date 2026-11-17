"""The `Contact` table."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.models.base import Base, TimestampMixin, UpdatedMixin, str_enum
from app.models.enums import (
    BuyerRole,
    EmailSource,
    EmailVerdict,
    PhoneSource,
)


class Contact(TimestampMixin, UpdatedMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("company_id", "first_name", "last_name"),
        CheckConstraint("email IS NULL OR email = lower(email)", name="email_lowercase"),
        CheckConstraint(
            "email IS NULL OR email_verdict = 'valid'", name="email_only_if_valid"
        ),  # never store unverified emails
        CheckConstraint("email IS NULL OR email_verified_at IS NOT NULL", name="email_has_verified_at"),
        Index("ix_contacts_email", "email"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    job_title: Mapped[str] = mapped_column(String(200))
    seniority: Mapped[str | None] = mapped_column(String(50))
    department: Mapped[str | None] = mapped_column(String(100))
    buyer_role: Mapped[BuyerRole | None] = mapped_column(str_enum(BuyerRole))
    person_source_url: Mapped[str] = mapped_column(String(2000))
    person_source_quote: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(320))
    email_source: Mapped[EmailSource | None] = mapped_column(str_enum(EmailSource))
    email_finder_status: Mapped[str | None] = mapped_column(String(40))  # raw provider status
    email_verdict: Mapped[EmailVerdict] = mapped_column(
        str_enum(EmailVerdict), default=EmailVerdict.NOT_CHECKED
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    direct_phone: Mapped[str | None] = mapped_column(String(20))
    direct_phone_source: Mapped[PhoneSource | None] = mapped_column(str_enum(PhoneSource))
    direct_phone_source_url: Mapped[str | None] = mapped_column(String(2000))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))

    @validates("email")
    def _lower_email(self, _key, value: str | None) -> str | None:
        return value.strip().lower() if value else value
