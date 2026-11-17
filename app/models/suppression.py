"""The `Suppression` table."""

from sqlalchemy import (
    CheckConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Suppression(TimestampMixin, Base):
    """Opt-outs and erasure requests (UK GDPR / DPDP). Suppressed domains are never researched again and
    suppressed emails are never looked up or delivered again."""

    __tablename__ = "suppressions"
    __table_args__ = (
        UniqueConstraint("kind", "value"),
        CheckConstraint("kind IN ('email', 'domain')", name="kind_valid"),
        CheckConstraint("value = lower(value)", name="value_lowercase"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    value: Mapped[str] = mapped_column(String(320))
    reason: Mapped[str] = mapped_column(String(300))
