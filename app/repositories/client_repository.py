"""Saved client profiles: read a client's website once, reuse it on every run until it goes stale."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config.database import session_scope
from app.models import ClientProfileRecord

PROFILE_MAX_AGE = timedelta(days=30)  # a client's own site rarely changes what it sells or where it is


def get_fresh(client_key: str, website: str | None) -> ClientProfileRecord | None:
    """The saved profile, unless it is stale or was read from a different website."""
    with session_scope() as s:
        record = s.scalar(select(ClientProfileRecord).where(ClientProfileRecord.client_key == client_key))
        if record is None or (website and record.website and record.website != website):
            return None
        read_at = record.read_at if record.read_at.tzinfo else record.read_at.replace(tzinfo=UTC)
        return record if datetime.now(UTC) - read_at <= PROFILE_MAX_AGE else None


def save(client_key: str, website: str | None, **fields) -> None:
    with session_scope() as s:
        record = s.scalar(select(ClientProfileRecord).where(ClientProfileRecord.client_key == client_key))
        if record is None:
            record = ClientProfileRecord(client_key=client_key)
            s.add(record)
        record.website = website
        record.read_at = datetime.now(UTC)
        for name, value in fields.items():
            setattr(record, name, value)


def forget(client_key: str) -> None:
    """Forces the next run to read the website again (e.g. the client changed what it sells)."""
    with session_scope() as s:
        if record := s.scalar(
            select(ClientProfileRecord).where(ClientProfileRecord.client_key == client_key)
        ):
            s.delete(record)
