"""The `ClientProfile` table: what we learned about a client from its own website, saved once."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedMixin


class ClientProfileRecord(TimestampMixin, UpdatedMixin, Base):
    """One row per client (keyed like jobs.client_key). Reading a client's site costs a model call over
    ~9k tokens; its facts change rarely, so they are read once and reused until they go stale."""

    __tablename__ = "client_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_key: Mapped[str] = mapped_column(String(100), unique=True)
    website: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    sells_how: Mapped[str] = mapped_column(Text, default="")
    offer: Mapped[str] = mapped_column(Text, default="")
    why_choose: Mapped[list] = mapped_column(
        JSON, default=list
    )  # only claims stated on the client's own site
    facts: Mapped[dict] = mapped_column(JSON, default=dict)  # verified ClientProfile (country, public sector)
    pages_read: Mapped[list] = mapped_column(JSON, default=list)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
