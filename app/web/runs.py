"""Background work started from the console: drafting a plan, and running a job.

Both are long (seconds for a draft, minutes for a run), so neither can happen inside a request. They run
as asyncio tasks in this process and report progress only through the database, which the pipeline
already writes to as it goes — so a reload, a second browser tab or a restart all show the same truth.

Single-process on purpose for now: one operator, a handful of runs a day. The job queue in Postgres
(docs/RESEARCH_ENGINE.md) is what replaces this when runs need to survive a restart or scale past one
machine; until then a crash mid-run leaves the job marked `failed` with whatever it had already saved.
"""
import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import get_settings
from app.core.cost import CostTracker
from app.core.fetcher import Fetcher
from app.core.normalize import normalize_domain
from app.db import session_scope
from app.models.enums import JobStatus
from app.models.tables import Job
from sqlalchemy import select
from app.pipeline.intake import draft_plan, plan_from_job, plan_to_json
from app.pipeline.jobspec import JobSpec
from app.pipeline.runner import run_job
from app.providers.llm.gemini_provider import GeminiProvider
from app.providers.llm.manual_provider import ManualLLMProvider
from app.providers.llm.openai_provider import OpenAIProvider

log = logging.getLogger("leadpro.web.runs")

MAX_CONCURRENT_RUNS = 2
MAX_CONCURRENT_DRAFTS = 3  # each draft is a paid model call: a held-down submit must not fan out
_tasks: dict[str, asyncio.Task] = {}  # keeps a strong reference so tasks are not garbage collected


def client_key(website: str | None, name: str) -> str:
    """Stable slug. Never-repeat suppression is per client key, so it must be the same every run: the
    registered domain is the most stable handle a client has."""
    base = normalize_domain(website or "") or name
    slug = re.sub(r"[^a-z0-9]+", "-", base.strip().lower()).strip("-")
    return slug[:100] or "client"


def active_runs() -> int:
    return sum(1 for key, task in _tasks.items() if key.startswith("run:") and not task.done())


def active_drafts() -> int:
    return sum(1 for key, task in _tasks.items() if key.startswith("draft:") and not task.done())


def is_busy(job_id: int) -> bool:
    task = _tasks.get(f"run:{job_id}") or _tasks.get(f"draft:{job_id}")
    return task is not None and not task.done()


def _forget(key: str) -> None:
    task = _tasks.get(key)
    if task is not None and task.done():
        _tasks.pop(key, None)


def _spawn(key: str, coro) -> None:
    for done_key in [k for k, t in _tasks.items() if t.done()]:
        _forget(done_key)
    task = asyncio.create_task(coro, name=key)
    _tasks[key] = task
    task.add_done_callback(lambda t: _log_task_result(key, t))


def _log_task_result(key: str, task: asyncio.Task) -> None:
    if task.cancelled():
        log.warning("%s was cancelled", key)
    elif exc := task.exception():
        log.error("%s failed: %s: %s", key, type(exc).__name__, exc)


def _set_failed(job_id: int, message: str) -> None:
    with session_scope() as s:
        if job := s.get(Job, job_id):
            job.status = JobStatus.FAILED
            job.error = message[:4000]
            job.finished_at = datetime.now(UTC)


def _llm(cost: CostTracker):
    """The provider for the console's own LLM step, or None when there is no way to call one."""
    settings = get_settings()
    if settings.llm_mode == "manual":
        return ManualLLMProvider(cost)
    if settings.llm_mode == "gemini":
        return GeminiProvider(cost) if settings.gemini_api_key else None
    return OpenAIProvider(cost) if settings.openai_api_key else None


def create_draft_job(website: str | None, ask: str, count: int, country: str, client_name: str,
                     freshness_days: int, personalized_email: bool, include_icp_only: bool,
                     cost_cap_inr: float) -> int:
    """Creates the job row in `researching_client` so the plan has somewhere to live before review."""
    with session_scope() as s:
        job = Job(client_key=client_key(website, client_name), client_name=client_name or
                  (normalize_domain(website or "") or "Client"), client_website=website or None, ask=ask,
                  target_countries=[country], requested_count=count, freshness_days=freshness_days,
                  personalized_email=personalized_email, cost_cap_inr=cost_cap_inr,
                  icp={"intake": {"include_icp_only": include_icp_only}},
                  status=JobStatus.RESEARCHING_CLIENT)
        s.add(job)
        s.flush()
        return job.id


async def draft(job_id: int) -> None:
    """Reads the client's site and writes the drafted plan back, leaving the job awaiting review."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        website, ask, count = job.client_website, job.ask, job.requested_count
        country, name, cap = (job.target_countries or ["GB"])[0], job.client_name, job.cost_cap_inr
        icp_only = bool((job.icp or {}).get("intake", {}).get("include_icp_only"))
        freshness, emails = job.freshness_days, job.personalized_email
    cost = CostTracker(cap)
    fetcher = Fetcher()
    try:
        plan = await draft_plan(website, ask, count, country, _llm(cost), fetcher, name)
        plan.freshness_days, plan.personalized_email = freshness, emails
        plan.include_icp_only, plan.cost_cap_inr = icp_only, cap
        with session_scope() as s:
            if job := s.get(Job, job_id):
                job.icp = plan_to_json(plan)
                job.status = JobStatus.AWAITING_ICP_REVIEW
                job.cost_inr = round(cost.spent_inr, 2)
                job.cost_lines = [vars(line) for line in cost.lines]
    except Exception as exc:
        log.exception("drafting the plan for job %s failed", job_id)
        _set_failed(job_id, f"Could not draft the research plan: {type(exc).__name__}: {exc}")
    finally:
        await fetcher.aclose()


def reopen_draft(job_id: int) -> bool:
    """Puts a plan awaiting review back into drafting. False if the job is not awaiting review."""
    if is_busy(job_id):
        return False
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None or job.status != JobStatus.AWAITING_ICP_REVIEW:
            return False
        job.status = JobStatus.RESEARCHING_CLIENT
        return True


def start_draft(job_id: int) -> None:
    _spawn(f"draft:{job_id}", draft(job_id))


async def run(job_id: int, spec: JobSpec) -> None:
    out_dir = Path(get_settings().web_output_dir)
    try:
        await run_job(spec, out_dir, job_id=job_id)
    except asyncio.CancelledError:
        # The server is shutting down mid-run: say so now, rather than leave it 'running' until the
        # startup sweep's staleness window expires. Everything already found is saved.
        await asyncio.shield(asyncio.to_thread(_set_failed, job_id, INTERRUPTED))
        raise
    except Exception as exc:  # run_job records its own failures; this is the last resort
        log.exception("run for job %s failed outside the pipeline", job_id)
        _set_failed(job_id, f"{type(exc).__name__}: {exc}")


async def shutdown(timeout_s: float = 10.0) -> None:
    """Cancels work still running when the server stops, giving each a moment to record its state."""
    pending = [t for t in _tasks.values() if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=timeout_s)


def start_run(job_id: int, spec: JobSpec) -> None:
    _spawn(f"run:{job_id}", run(job_id, spec))


# A run that has not changed stage for this long is no longer being driven by anyone: the process that
# owned it restarted or died. Drafting takes seconds; a single pipeline stage, well under an hour.
STALE_DRAFT = timedelta(minutes=15)
STALE_RUN = timedelta(hours=3)
ACTIVE = (JobStatus.QUEUED, JobStatus.DISCOVERING, JobStatus.RESEARCHING, JobStatus.SCORING,
          JobStatus.FINDING_CONTACTS, JobStatus.REVALIDATING)
INTERRUPTED = ("Interrupted: the server restarted while this run was going, so it stopped. Everything it "
               "found before that is saved below. Start a new run to continue.")


def sweep_orphans() -> int:
    """Marks runs nobody is driving any more as failed, so they stop showing as in flight forever.
    Never touches a run this process is driving; a run from another process (the CLI) is only swept
    once it has sat in one stage far longer than any stage takes."""
    now = datetime.now(UTC)
    swept = 0
    with session_scope() as s:
        jobs = s.scalars(select(Job).where(Job.status.in_((JobStatus.RESEARCHING_CLIENT, *ACTIVE)))).all()
        for job in jobs:
            if is_busy(job.id):
                continue
            limit = STALE_DRAFT if job.status == JobStatus.RESEARCHING_CLIENT else STALE_RUN
            changed = job.updated_at or job.created_at
            if changed and changed.tzinfo is None:
                changed = changed.replace(tzinfo=UTC)
            if changed and now - changed > limit:
                job.status, job.error, job.finished_at = JobStatus.FAILED, INTERRUPTED, now
                swept += 1
    if swept:
        log.warning("marked %d orphaned run(s) as interrupted", swept)
    return swept


def orphaned_drafts() -> list[int]:
    """Plans that were being drafted when the process stopped. Only this process drafts, so at startup
    every one of them has lost its task."""
    with session_scope() as s:
        ids = s.scalars(select(Job.id).where(Job.status == JobStatus.RESEARCHING_CLIENT)).all()
    return [i for i in ids if not is_busy(i)]


def plan_for_review(job: Job):
    return plan_from_job(job)
