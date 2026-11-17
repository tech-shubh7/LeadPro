"""The `SourceDocument` table: the exact text a quote was verified against, kept so it can be re-proven."""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SourceDocument(TimestampMixin, Base):
    """An immutable snapshot of one source's text at the moment evidence was taken from it. Evidence points
    here with the quote's character offset, so 'the quote is on the page' stays provable after the live
    page changes or disappears. Identical text is stored once (url + content hash)."""

    __tablename__ = "source_documents"
    __table_args__ = (UniqueConstraint("url", "content_hash"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2000))
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 of `text`
    text: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
