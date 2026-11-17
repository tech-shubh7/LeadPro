"""Opt-outs and erasure requests (UK GDPR / DPDP), actioned from the console."""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.status import HTTP_303_SEE_OTHER

from app.config.database import DbSession
from app.config.templates import render
from app.exceptions.custom_exceptions import BadRequestError
from app.middlewares.csrf import check_csrf
from app.repositories import console_repository, lead_repository

router = APIRouter(prefix="/suppressions", tags=["Compliance"])
REASON_MAX = 300


@router.get("", response_class=HTMLResponse)
def suppressions(
    request: Request, session: DbSession, page: int = Query(1, ge=1), q: str = Query(""), done: str = ""
) -> Response:
    return render(
        request,
        "suppressions.html",
        nav="suppressions",
        done=done,
        q=q,
        rows=console_repository.suppressions_page(session, page, q),
    )


@router.post("")
def add_suppression(
    request: Request,
    session: DbSession,
    kind: Annotated[str, Form()],
    value: Annotated[str, Form()],
    reason: Annotated[str, Form()],
    csrf: Annotated[str, Form()] = "",
) -> Response:
    check_csrf(request, csrf)
    if kind not in ("domain", "email") or not value.strip() or not reason.strip():
        raise BadRequestError("Pick domain or email, and give a value and a reason.")
    lead_repository.add_suppression(session, kind, value, reason.strip()[:REASON_MAX])
    session.commit()
    return RedirectResponse(f"/suppressions?done=Suppressed+this+{kind}.", status_code=HTTP_303_SEE_OTHER)


@router.post("/erase")
def erase(
    request: Request,
    session: DbSession,
    email: Annotated[str, Form()],
    reason: Annotated[str, Form()],
    csrf: Annotated[str, Form()] = "",
) -> Response:
    """Deletes the stored contact rows for the address and suppresses it for every future run."""
    check_csrf(request, csrf)
    if "@" not in email or not reason.strip():
        raise BadRequestError("Give a valid email address and a reason.")
    erased = lead_repository.erase_person_by_email(session, email, reason.strip()[:REASON_MAX])
    session.commit()
    return RedirectResponse(
        f"/suppressions?done=Erased+{erased}+contact+record(s)+and+suppressed+the+address.",
        status_code=HTTP_303_SEE_OTHER,
    )
