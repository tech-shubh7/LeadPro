"""Optional access-key gate for the console (WEB_AUTH_TOKEN). Off when no key is configured.

The cookie holds a keyed hash of the key, never the key; wrong keys are throttled per address.
"""

import hashlib
import hmac
import secrets
import time

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.status import HTTP_303_SEE_OTHER

from app.config.templates import base_context, templates

AUTH_COOKIE = "lp_auth"
OPEN_PATHS = ("/static/", "/healthz")
SESSION_DAYS = 30
SIGNIN_WINDOW_S = 900
SIGNIN_MAX_FAILURES = 10
_signin_failures: dict[str, list[float]] = {}


def session_value(token: str) -> str:
    """A copied cookie or a log line does not reveal the key, and rotating the key signs everyone out."""
    return hmac.new(token.encode(), b"leadpro-console-session-v1", hashlib.sha256).hexdigest()


def local_path(target: str) -> str:
    """Only a path on this site: '//evil.com' and '/\\evil.com' are other sites to a browser."""
    return target if target.startswith("/") and not target.startswith(("//", "/\\")) else "/"


def signin_throttled(who: str) -> bool:
    """In-process, which matches the single-process deployment."""
    now = time.monotonic()
    if len(_signin_failures) > 10_000:  # many addresses: drop the ones whose window has passed
        for key in [k for k, ts in _signin_failures.items() if not ts or now - ts[-1] >= SIGNIN_WINDOW_S]:
            del _signin_failures[key]
    recent = [t for t in _signin_failures.get(who, []) if now - t < SIGNIN_WINDOW_S]
    _signin_failures[who] = recent
    return len(recent) >= SIGNIN_MAX_FAILURES


def _signin_failed(who: str) -> None:
    _signin_failures.setdefault(who, []).append(time.monotonic())


def _signin_page(request: Request, status_code: int, **context) -> Response:
    return templates.TemplateResponse(
        request, "signin.html", base_context(request) | context, status_code=status_code
    )


def _signed_in(request: Request, target: str, token: str) -> Response:
    response = RedirectResponse(target, status_code=HTTP_303_SEE_OTHER)
    response.set_cookie(
        AUTH_COOKIE,
        session_value(token),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=SESSION_DAYS * 86400,
    )
    return response


async def authenticate(request: Request, token: str) -> Response | None:
    """None when the request may proceed; otherwise the response to send instead (sign-in page or
    redirect after signing in)."""
    if not token or request.url.path.startswith(OPEN_PATHS):
        return None
    if request.url.path == "/signin" and request.method == "POST":
        who = request.client.host if request.client else "?"
        if signin_throttled(who):
            return _signin_page(request, 429, bad=True, throttled=True, next="/")
        form = await request.form()
        target = local_path(str(form.get("next") or "/"))
        if not secrets.compare_digest(str(form.get("token", "")), token):
            _signin_failed(who)
            return _signin_page(request, 401, bad=True, next=target)
        return _signed_in(request, target, token)
    query_token = request.query_params.get("token", "")
    if query_token and secrets.compare_digest(query_token, token):  # sign in once by link, then a cookie
        return _signed_in(request, str(request.url.remove_query_params("token")), token)
    if secrets.compare_digest(request.cookies.get(AUTH_COOKIE, ""), session_value(token)):
        return None
    wanted = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return _signin_page(request, 401, next=wanted if request.method == "GET" else "/")
