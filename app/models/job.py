"""The `Job` table."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedMixin, str_enum
from app.models.enums import (
    JobStatus,
)


class Job(TimestampMixin, UpdatedMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("requested_count > 0", name="requested_count_positive"),
        CheckConstraint("freshness_days BETWEEN 1 AND 365", name="freshness_days_range"),
        CheckConstraint("cost_inr >= 0 AND cost_cap_inr > 0", name="cost_non_negative"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    client_key: Mapped[str] = mapped_column(String(100), index=True)  # stable slug, used for never-repeat
    client_name: Mapped[str] = mapped_column(String(200))
    client_website: Mapped[str | None] = mapped_column(String(500))
    ask: Mapped[str] = mapped_column(Text)
    target_countries: Mapped[list] = mapped_column(JSON)  # e.g. ["GB"]
    requested_count: Mapped[int] = mapped_column(Integer)
    freshness_days: Mapped[int] = mapped_column(Integer, default=90)
    personalized_email: Mapped[bool] = mapped_column(Boolean, default=False)
    icp: Mapped[dict] = mapped_column(JSON, default=dict)  # ICP + signal playbook (from the job YAML)
    suppressed_domains: Mapped[list] = mapped_column(JSON, default=list)  # client's customers/competitors
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), default=JobStatus.QUEUED)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_inr: Mapped[float] = mapped_column(Float, default=0.0)
    cost_cap_inr: Mapped[float] = mapped_column(Float)
    cost_lines: Mapped[list] = mapped_column(JSON, default=list)  # per-provider usage
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Run queue (run_queue_repository): the reviewed spec to execute, and the worker lease holding it.
    # Only rows with a run_spec are ever claimed, so a run started from the CLI is never taken over.
    run_spec: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True)
    )  # None must be SQL NULL, not JSON null
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
