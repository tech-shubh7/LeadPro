"""The one HTTP middleware: CSRF token, then the access gate, then the handler, then response headers.
Composed here so the order (which matters) is explicit in one place."""

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response

from app.middlewares.auth import authenticate
from app.middlewares.csrf import prepare_token, set_token_cookie
from app.middlewares.security_headers import apply_security_headers


async def request_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    minted_csrf = prepare_token(request)
    response = await authenticate(request, request.app.state.auth_token)
    if response is None:
        response = await call_next(request)
    apply_security_headers(request, response)
    if minted_csrf and not request.url.path.startswith("/static/"):
        set_token_cookie(request, response, minted_csrf)
    return response
