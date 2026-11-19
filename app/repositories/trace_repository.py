"""Model-call traces: saving each one as it happens, and the reads behind the Trace and Observability views."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config.database import session_scope
from app.config.settings import get_settings
from app.models import LlmCall
from app.providers.llm.llm_trace import CallTrace
from app.repositories.console_repository import Page, _paginate

TRACE_PAGE_SIZE = 50


def save_call(trace: CallTrace) -> None:
    """The run's trace sink: one row per model call, written as the call finishes (runs in a worker thread)."""
    with session_scope() as s:
        s.add(LlmCall(**asdict(trace)))


def calls_page(session: Session, job_id: int, page: int, stage: str = "", domain: str = "") -> Page:
    stmt = select(LlmCall).where(LlmCall.job_id == job_id).order_by(LlmCall.id)
    if stage:
        stmt = stmt.where(LlmCall.stage == stage)
    if domain:
        stmt = stmt.where(LlmCall.domain == domain)
    result = _paginate(session, stmt, page, TRACE_PAGE_SIZE)
    return Page([row[0] for row in result.items], result.total, result.page, result.size)


def recent_calls(session: Session, limit: int = 30) -> list[LlmCall]:
    return list(session.scalars(select(LlmCall).order_by(LlmCall.id.desc()).limit(limit)))


def get_call(session: Session, call_id: int) -> LlmCall | None:
    return session.get(LlmCall, call_id)


def prune_traces() -> int:
    """Deletes traces older than trace_keep_days. Returns how many were removed."""
    cutoff = datetime.now(UTC) - timedelta(days=get_settings().trace_keep_days)
    with session_scope() as s:
        return s.execute(delete(LlmCall).where(LlmCall.created_at < cutoff)).rowcount or 0


@dataclass(frozen=True)
class CallStats:
    calls: int = 0
    errors: int = 0
    cached: int = 0
    avg_ms: int = 0
    max_ms: int = 0
    slowest_stage: str = ""


def call_stats(session: Session, job_id: int | None = None) -> CallStats:
    """Latency and errors of traced calls (one run, or every run when job_id is None)."""
    where = [LlmCall.job_id == job_id] if job_id is not None else []
    calls, errors, cached, avg_ms, max_ms = session.execute(
        select(
            func.count(LlmCall.id),
            func.count(LlmCall.id).filter(LlmCall.status == "error"),
            func.count(LlmCall.id).filter(LlmCall.from_cache.is_(True)),
            func.coalesce(func.avg(LlmCall.duration_ms), 0),
            func.coalesce(func.max(LlmCall.duration_ms), 0),
        ).where(*where)
    ).one()
    slowest = session.scalar(
        select(LlmCall.stage)
        .where(*where)
        .group_by(LlmCall.stage)
        .order_by(func.avg(LlmCall.duration_ms).desc())
    )
    return CallStats(int(calls), int(errors), int(cached), int(avg_ms), int(max_ms), slowest or "")


def stages_for(session: Session, job_id: int) -> list[str]:
    return list(
        session.scalars(
            select(LlmCall.stage).where(LlmCall.job_id == job_id).distinct().order_by(LlmCall.stage)
        )
    )
