from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.status import HTTP_303_SEE_OTHER

from app.config.database import DbSession
from app.config.templates import render
from app.exceptions.custom_exceptions import BadRequestError, NotFoundError
from app.middlewares.csrf import check_csrf
from app.models.lead_feedback import VERDICTS
from app.repositories import console_repository, feedback_repository

router = APIRouter(prefix="/leads", tags=["Leads"])


@router.get("/{lead_id}", response_class=HTMLResponse)
def lead(request: Request, session: DbSession, lead_id: int) -> Response:
    """Everything behind one lead: signals with their quotes and dates, people, drafts, company facts."""
    detail = console_repository.lead_detail(session, lead_id)
    if detail is None:
        raise NotFoundError()
    return render(
        request, "lead.html", d=detail, nav="runs", feedback=feedback_repository.for_lead(session, lead_id)
    )


@router.post("/{lead_id}/feedback")
def give_feedback(
    request: Request,
    session: DbSession,
    lead_id: int,
    verdict: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
) -> Response:
    """A person's verdict on the lead: kept with its evidence, the start of a real accuracy dataset."""
    check_csrf(request, csrf)
    if verdict not in VERDICTS:
        raise BadRequestError("Pick one of the verdicts.")
    if console_repository.lead_detail(session, lead_id) is None:
        raise NotFoundError()
    feedback_repository.save(session, lead_id, verdict, note)
    session.commit()
    return RedirectResponse(f"/leads/{lead_id}#feedback", status_code=HTTP_303_SEE_OTHER)
