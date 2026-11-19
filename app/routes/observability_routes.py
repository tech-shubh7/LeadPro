"""Observability: where every run's money and tokens went, and the exact prompt and answer of each model call."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.config.database import DbSession
from app.config.settings import get_settings
from app.config.templates import render
from app.exceptions.custom_exceptions import NotFoundError
from app.repositories import trace_repository
from app.services import observability_service

router = APIRouter(tags=["Observability"])


@router.get("/observability", response_class=HTMLResponse)
def observability(request: Request, session: DbSession) -> Response:
    total, runs = observability_service.workspace(session)
    return render(
        request,
        "observability.html",
        nav="observability",
        total=total,
        runs=runs,
        stats=trace_repository.call_stats(session),
        recent=trace_repository.recent_calls(session),
        config=observability_service.configuration(),
    )


@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(request: Request, session: DbSession, call_id: int) -> Response:
    call = trace_repository.get_call(session, call_id)
    if call is None:
        raise NotFoundError()
    return render(request, "call.html", nav="observability", c=call, max_chars=get_settings().trace_max_chars)
