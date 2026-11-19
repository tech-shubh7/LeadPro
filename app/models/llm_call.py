"""The `LlmCall` table: one row per model call, for the console's Trace and Observability views."""

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class LlmCall(TimestampMixin, Base):
    """What was asked, what came back, what it cost and how long it took. Prompts can contain page text that
    names people, so rows are deleted after settings.trace_keep_days (pruned at start-up)."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_job_stage", "job_id", "stage"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    # Null for calls outside a run (scripts, evaluations). Deleting a run deletes its traces.
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(30), default="")  # plan, discovery, extract, intent, score, ...
    channel: Mapped[str] = mapped_column(String(40), default="")  # the source that found the company
    domain: Mapped[str] = mapped_column(String(255), default="")  # the company being researched
    provider: Mapped[str] = mapped_column(String(20), default="")  # openai, gemini, manual, cache
    model: Mapped[str] = mapped_column(String(80))
    schema_name: Mapped[str] = mapped_column(
        String(80)
    )  # which answer was asked for (ExtractOut, ScoreOut...)
    effort: Mapped[str] = mapped_column(String(10), default="")
    status: Mapped[str] = mapped_column(String(10))  # ok | error
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)  # includes reasoning_tokens
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_inr: Mapped[float] = mapped_column(Float, default=0.0)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    instructions: Mapped[str] = mapped_column(Text, default="")
    input_text: Mapped[str] = mapped_column(Text, default="")
    output_text: Mapped[str] = mapped_column(Text, default="")
