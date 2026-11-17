"""CSRF protection by double-submit cookie: a cross-site form post cannot read the cookie to copy it."""

import secrets

from fastapi import Request
from fastapi.responses import Response

from app.config.templates import CSRF_COOKIE
from app.exceptions.custom_exceptions import BadRequestError


def prepare_token(request: Request) -> str | None:
    """Puts the request's token on `request.state` before anything renders. Returns a newly minted token
    (to be set as a cookie on the response) or None if the visitor already had one: the token must exist
    while the first page renders, or a first visit's first form post is rejected as stale."""
    existing = request.cookies.get(CSRF_COOKIE)
    minted = None if existing else secrets.token_urlsafe(32)
    request.state.csrf = existing or minted
    return minted


def set_token_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE, token, httponly=False, samesite="strict", secure=request.url.scheme == "https"
    )


def check_csrf(request: Request, submitted: str) -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not cookie or not submitted or not secrets.compare_digest(cookie, submitted):
        raise BadRequestError("This form went stale. Reload the page and try again.")
