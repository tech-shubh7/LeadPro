"""The run queue, in Postgres: queued runs are claimed by workers under a lease.

A worker claims one run in a single atomic statement (FOR UPDATE SKIP LOCKED: two workers never get the
same run), then heartbeats to extend its lease while it works. If the worker dies, the lease expires and
the next claim picks the run up again. Only rows with a `run_spec` (queued from the console) are ever
claimed, so a run started directly from the CLI is never taken over.
"""

from sqlalchemy import bindparam, delete, text

from app.config.database import session_scope
from app.models import Delivery, Evidence, Job, JobLead
from app.models.enums import JobStatus

# Statuses a run passes through while a worker drives it. A lease that expires in one of these means
# the worker died mid-run.
RUNNING = (
    JobStatus.QUEUED,
    JobStatus.DISCOVERING,
    JobStatus.RESEARCHING,
    JobStatus.SCORING,
    JobStatus.FINDING_CONTACTS,
    JobStatus.REVALIDATING,
)
_RUNNING_VALUES = [s.value for s in RUNNING]

_CLAIM = text(
    """
    WITH next AS (
        SELECT id FROM jobs
        WHERE run_spec IS NOT NULL AND attempts < :max_attempts
          AND status IN :running
          AND (locked_until IS NULL OR locked_until < now())
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE jobs SET locked_by = :worker, locked_until = now() + make_interval(secs => :lease),
                    heartbeat_at = now(), attempts = jobs.attempts + 1
    FROM next WHERE jobs.id = next.id
    RETURNING jobs.id, jobs.run_spec, jobs.attempts
"""
).bindparams(bindparam("running", expanding=True))


def enqueue(job_id: int, run_spec: dict) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.run_spec, job.status, job.attempts = run_spec, JobStatus.QUEUED, 0
        job.locked_by = job.locked_until = None
        job.error, job.finished_at = None, None


def claim(worker: str, lease_seconds: int, max_attempts: int) -> tuple[int, dict, int] | None:
    """(job id, run spec, attempt number) of the next run to execute, now leased to `worker`; or None."""
    with session_scope() as s:
        row = s.execute(
            _CLAIM,
            {
                "worker": worker,
                "lease": lease_seconds,
                "max_attempts": max_attempts,
                "running": _RUNNING_VALUES,
            },
        ).first()
        return (row[0], row[1], row[2]) if row else None


def heartbeat(job_id: int, worker: str, lease_seconds: int) -> bool:
    """Extends the lease. False if this worker no longer holds it (another worker reclaimed the run)."""
    with session_scope() as s:
        result = s.execute(
            text(
                "UPDATE jobs SET locked_until = now() + make_interval(secs => :lease), heartbeat_at = now() "
                "WHERE id = :id AND locked_by = :worker"
            ),
            {"id": job_id, "worker": worker, "lease": lease_seconds},
        )
        return result.rowcount == 1


def release(job_id: int, worker: str) -> None:
    with session_scope() as s:
        s.execute(
            text("UPDATE jobs SET locked_by = NULL, locked_until = NULL WHERE id = :id AND locked_by = :w"),
            {"id": job_id, "w": worker},
        )


def requeue(job_id: int, worker: str) -> None:
    """A graceful stop (deploy, restart): back in the queue, without spending one of its attempts."""
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE jobs SET status = 'queued', locked_by = NULL, locked_until = NULL, "
                "attempts = GREATEST(attempts - 1, 0) WHERE id = :id AND locked_by = :w"
            ),
            {"id": job_id, "w": worker},
        )


def clear_partial_results(job_id: int, deliveries: bool = False) -> None:
    """Before a retry or a continue: this run's leads and evidence, so nothing is counted twice. A continue
    also clears the run's own delivery records, or its leads would be skipped as already delivered when it
    runs again. Contacts and companies are shared across runs and kept; spend stays on the job and counts."""
    with session_scope() as s:
        s.execute(delete(Evidence).where(Evidence.job_id == job_id))
        s.execute(delete(JobLead).where(JobLead.job_id == job_id))
        if deliveries:
            s.execute(delete(Delivery).where(Delivery.job_id == job_id))


def give_up_exhausted(max_attempts: int) -> list[int]:
    """Runs whose lease expired with no attempts left: marked failed, with why."""
    with session_scope() as s:
        rows = s.execute(
            text(
                """
            UPDATE jobs SET status = 'failed', locked_by = NULL, locked_until = NULL, finished_at = now(),
                   error = 'Stopped after ' || attempts || ' attempts: the worker running it stopped each time.'
            WHERE run_spec IS NOT NULL AND attempts >= :max AND status IN :running
              AND (locked_until IS NULL OR locked_until < now())
            RETURNING id"""
            ).bindparams(bindparam("running", expanding=True)),
            {"max": max_attempts, "running": _RUNNING_VALUES},
        ).all()
        return [r[0] for r in rows]


def queue_position(job_id: int) -> int:
    """How many queued runs are ahead of this one (0 = next or running)."""
    with session_scope() as s:
        return (
            s.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE status = 'queued' AND locked_by IS NULL AND run_spec IS NOT NULL "
                    "AND id < :id"
                ),
                {"id": job_id},
            ).scalar()
            or 0
        )


def continue_with_budget(job_id: int, new_cap_inr: float) -> str | None:
    """Re-queues a run the budget stopped, with a bigger cap. Returns why it cannot, or None when queued."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None or not job.run_spec:
            return "This run cannot be continued (it was not started from the console)."
        if not (job.progress or {}).get("stopped_by_cap"):
            return "Only a run that stopped because it reached its budget can be continued."
        if new_cap_inr <= job.cost_cap_inr:
            return f"The new budget must be more than the current ₹{job.cost_cap_inr:,.0f}."
        job.cost_cap_inr = new_cap_inr
        job.run_spec = {**job.run_spec, "cost_cap_inr": new_cap_inr}
    clear_partial_results(job_id, deliveries=True)
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status, job.attempts, job.locked_by, job.locked_until = JobStatus.QUEUED, 0, None, None
        job.error, job.finished_at = None, None
    return None
