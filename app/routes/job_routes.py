"""One run: its leads, what was dropped and why, the plan and cost, live progress and downloads."""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.status import HTTP_303_SEE_OTHER

from app.config.database import DbSession
from app.config.templates import render
from app.exceptions.custom_exceptions import BadRequestError, NotFoundError
from app.middlewares.csrf import check_csrf
from app.models.enums import JobStatus
from app.repositories import console_repository as repo
from app.repositories import feedback_repository, run_queue_repository
from app.schemas.job_spec_schema import MAX_COST_CAP_INR
from app.services import run_manager_service
from app.services.export_service import job_files

router = APIRouter(prefix="/jobs", tags=["Runs"])
TABS = ("leads", "rejected", "method")


@router.get("/{job_id}", response_class=HTMLResponse)
def job_detail(
    request: Request,
    session: DbSession,
    job_id: int,
    page: int = Query(1, ge=1),
    tier: str = "",
    contact: str = "",
    q: str = Query(""),
    tab: str = "leads",
) -> Response:
    job = repo.get_job(session, job_id)
    if job is None:
        raise NotFoundError()
    if job.status in (JobStatus.RESEARCHING_CLIENT, JobStatus.AWAITING_ICP_REVIEW):
        return RedirectResponse(f"/new/{job_id}", status_code=HTTP_303_SEE_OTHER)
    tab = tab if tab in TABS else "leads"
    leads = repo.job_leads_page(session, job_id, page, tier, contact, q) if tab == "leads" else None
    rated = feedback_repository.verdicts_for_leads(session, [r.lead.id for r in leads.items]) if leads else {}
    return render(
        request,
        "job.html",
        job=job,
        nav="runs",
        tab=tab,
        tier=tier,
        contact=contact,
        q=q,
        leads=leads,
        rated=rated,
        feedback=feedback_repository.summary_for_job(session, job_id),
        rejected=repo.job_rejections_page(session, job_id, page) if tab == "rejected" else None,
        counts=repo.lead_counts_for_jobs(session, [job_id]).get(job_id, repo.LeadCounts()),
        reasons=repo.rejection_reasons(session, job),
        costs=repo.cost_breakdown(job),
        files=job_files(request.app.state.output_dir, job.client_key, job.id),
        plan=run_manager_service.plan_for_review(job),
        running=job.status in repo.RUNNING_STATUSES,
    )


@router.post("/{job_id}/continue")
async def continue_run(
    request: Request, job_id: int, cost_cap_inr: Annotated[float, Form()], csrf: Annotated[str, Form()] = ""
) -> Response:
    """Continues a run the budget stopped, with a bigger budget. Work already done is replayed from caches
    (pages, model answers, verified emails), so only new research is paid for."""
    check_csrf(request, csrf)
    if not 0 < cost_cap_inr <= MAX_COST_CAP_INR:
        raise BadRequestError(f"Choose a budget up to ₹{MAX_COST_CAP_INR:,.0f}.")
    if problem := await run_in_threadpool(run_queue_repository.continue_with_budget, job_id, cost_cap_inr):
        raise BadRequestError(problem)
    run_manager_service.wake_worker()
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.get("/{job_id}/progress")
def progress(session: DbSession, job_id: int) -> JSONResponse:
    data = repo.job_progress(session, job_id)
    if data is None:
        raise NotFoundError()
    return JSONResponse(data)


@router.get("/{job_id}/files/{name}")
def download(request: Request, session: DbSession, job_id: int, name: str) -> FileResponse:
    """Only files this job actually produced, matched by name: a path from the URL is never joined onto
    the output directory."""
    job = repo.get_job(session, job_id)
    if job is None:
        raise NotFoundError()
    match = next(
        (p for p in job_files(request.app.state.output_dir, job.client_key, job.id) if p.name == name), None
    )
    if match is None:
        raise NotFoundError()
    return FileResponse(match, filename=match.name)
