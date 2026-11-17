"""The `Delivery` table."""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Delivery(Base):
    __tablename__ = "deliveries"
    # NULLS NOT DISTINCT: a company-only delivery (no contact) can't be recorded twice either
    __table_args__ = (
        UniqueConstraint("client_key", "company_id", "contact_id", postgresql_nulls_not_distinct=True),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    client_key: Mapped[str] = mapped_column(String(100), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
