"""New run: ask -> drafted plan -> operator review -> start. Nothing is spent on research until the
reviewed plan is started, because a wrong plan is the one error no downstream check can catch."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.status import HTTP_303_SEE_OTHER

from app.config.constants import SIGNAL_STRENGTHS
from app.config.database import DbSession
from app.config.settings import get_settings
from app.config.templates import render
from app.exceptions.custom_exceptions import BadRequestError, NotFoundError
from app.middlewares.csrf import check_csrf
from app.models.enums import JobStatus
from app.repositories import client_repository, console_repository
from app.services import run_manager_service as runs
from app.services.intake_service import (
    ReviewedPlanForm,
    build_reviewed_spec,
    parse_output_mode,
    validate_intake_form,
)

router = APIRouter(prefix="/new", tags=["New run"])
# Repeated form fields (one per signal on the review page); empty when none were sent.
StrList = Annotated[list[str], Form(default_factory=list)]


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=HTTP_303_SEE_OTHER)


def _intake_page(request: Request, form: dict, errors: dict, status_code: int = 200) -> Response:
    settings = get_settings()
    return render(
        request,
        "new.html",
        nav="new",
        form=form,
        errors=errors,
        status_code=status_code,
        default_cap=settings.max_cost_per_job_inr,
        default_freshness=settings.freshness_days,
    )


@router.get("", response_class=HTMLResponse)
def new_run(request: Request) -> Response:
    return _intake_page(request, form={}, errors={})


@router.post("")
async def create_run(
    request: Request,
    website: Annotated[str, Form()] = "",
    ask: Annotated[str, Form()] = "",
    count: Annotated[int, Form()] = 25,
    country: Annotated[str, Form()] = "GB",
    client_name: Annotated[str, Form()] = "",
    freshness_days: Annotated[int, Form()] = 90,
    personalized_email: Annotated[bool, Form()] = False,
    include_icp_only: Annotated[bool, Form()] = False,
    output_mode: Annotated[str, Form()] = "contacts",
    cost_cap_inr: Annotated[float, Form()] = 0.0,
    refresh_client: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form()] = "",
) -> Response:
    """Validates the ask, then drafts the plan in the background."""
    check_csrf(request, csrf)
    cap = cost_cap_inr or get_settings().max_cost_per_job_inr
    form = {
        "website": website,
        "ask": ask,
        "count": count,
        "country": country,
        "client_name": client_name,
        "freshness_days": freshness_days,
        "personalized_email": personalized_email,
        "include_icp_only": include_icp_only,
        "output_mode": output_mode,
        "cost_cap_inr": cap,
    }
    if errors := validate_intake_form(
        website, ask, count, country, freshness_days, cap, client_name, output_mode
    ):
        return _intake_page(request, form, errors, status_code=400)
    if runs.active_drafts() >= runs.MAX_CONCURRENT_DRAFTS:
        busy = {"website": "A few plans are already being drafted. Try again in a minute."}
        return _intake_page(request, form, busy, status_code=429)
    if refresh_client:  # the client changed what it sells: read its website again
        await run_in_threadpool(client_repository.forget, runs.client_key(website, client_name))
    job_id = await run_in_threadpool(
        runs.create_draft_job,
        website.strip(),
        ask.strip(),
        count,
        country.upper(),
        client_name.strip(),
        freshness_days,
        personalized_email,
        include_icp_only,
        cap,
        parse_output_mode(output_mode),  # validated above
    )
    runs.start_draft(job_id)
    return _see_other(f"/new/{job_id}")


@router.get("/{job_id}", response_class=HTMLResponse)
def review(request: Request, session: DbSession, job_id: int) -> Response:
    job = console_repository.get_job(session, job_id)
    if job is None:
        raise NotFoundError()
    if job.status == JobStatus.RESEARCHING_CLIENT:
        return render(request, "drafting.html", nav="new", job=job)
    if job.status != JobStatus.AWAITING_ICP_REVIEW:
        return _see_other(f"/jobs/{job_id}")
    return render(
        request,
        "review.html",
        nav="new",
        job=job,
        plan=runs.plan_for_review(job),
        strengths=SIGNAL_STRENGTHS,
        busy=False,
        max_runs=runs.MAX_CONCURRENT_RUNS,
    )


@router.get("/{job_id}/status")
def draft_status(session: DbSession, job_id: int) -> JSONResponse:
    status = console_repository.draft_status(session, job_id)
    if status is None:
        raise NotFoundError()
    return JSONResponse(status)


@router.post("/{job_id}/redraft")
async def redraft(request: Request, job_id: int, csrf: Annotated[str, Form()] = "") -> Response:
    """Drafts the plan again, e.g. after the model was overloaded and a generic template came back."""
    check_csrf(request, csrf)
    if await run_in_threadpool(runs.reopen_draft, job_id):
        runs.start_draft(job_id)
    return _see_other(f"/new/{job_id}")


@router.post("/{job_id}/start")
async def start(
    request: Request,
    job_id: int,
    *,
    ask: Annotated[str, Form()] = "",
    count: Annotated[int, Form()] = 25,
    freshness_days: Annotated[int, Form()] = 90,
    cost_cap_inr: Annotated[float, Form()] = 0.0,
    personalized_email: Annotated[bool, Form()] = False,
    include_icp_only: Annotated[bool, Form()] = False,
    output_mode: Annotated[str, Form()] = "contacts",
    industries: Annotated[str, Form()] = "",
    geographies: Annotated[str, Form()] = "",
    size_band: Annotated[str, Form()] = "",
    business_model: Annotated[str, Form()] = "",
    why_they_buy: Annotated[str, Form()] = "",
    disqualifiers: Annotated[str, Form()] = "",
    buyer_titles: Annotated[str, Form()] = "",
    negative: Annotated[str, Form()] = "",
    offer: Annotated[str, Form()] = "",
    why_choose: Annotated[str, Form()] = "",
    signal_type: StrList,
    signal_description: StrList,
    signal_strength: StrList,
    signal_kind: StrList,
    signal_queries: StrList,
    csrf: Annotated[str, Form()] = "",
) -> Response:
    """Starts the run from the reviewed plan, as edited."""
    check_csrf(request, csrf)
    job = await run_in_threadpool(console_repository.load_job, job_id)
    if job is None:
        raise NotFoundError()
    if runs.is_busy(job_id) or job.status != JobStatus.AWAITING_ICP_REVIEW:
        return _see_other(f"/jobs/{job_id}")  # already started (double click, back button): show it
    mode = parse_output_mode(output_mode)
    if mode is None:
        raise BadRequestError("Pick what to deliver: companies with signals, or with verified contacts.")
    form = ReviewedPlanForm(
        ask=ask,
        count=count,
        freshness_days=freshness_days,
        cost_cap_inr=cost_cap_inr,
        personalized_email=personalized_email,
        include_icp_only=include_icp_only,
        output_mode=mode,
        industries=industries,
        geographies=geographies,
        size_band=size_band,
        business_model=business_model,
        why_they_buy=why_they_buy,
        disqualifiers=disqualifiers,
        buyer_titles=buyer_titles,
        negative=negative,
        offer=offer,
        why_choose=why_choose,
        signal_type=signal_type,
        signal_description=signal_description,
        signal_strength=signal_strength,
        signal_kind=signal_kind,
        signal_queries=signal_queries,
    )
    spec = build_reviewed_spec(job, form)
    await run_in_threadpool(runs.start_run, job_id, spec)  # queued: the worker picks it up
    runs.wake_worker()
    return _see_other(f"/jobs/{job_id}")
