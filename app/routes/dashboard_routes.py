from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.config.database import DbSession
from app.config.templates import render
from app.models.enums import JobStatus
from app.repositories import console_repository
from app.services import run_manager_service

router = APIRouter(tags=["Dashboard"])


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, session: DbSession, page: int = Query(1, ge=1), q: str = Query(""), status: str = ""
) -> Response:
    """Every run, newest first, with the workspace totals."""
    run_manager_service.sweep_orphans()
    status = status if status in set(JobStatus) else ""
    return render(
        request,
        "dashboard.html",
        nav="runs",
        q=q,
        status=status,
        totals=console_repository.totals(session),
        jobs=console_repository.jobs_page(session, page, q, status),
        statuses=[s.value for s in JobStatus],
    )
