"""Every error becomes a page a person can act on, or JSON for the polling endpoints. Never a raw
framework body, never a stack trace."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config.templates import render
from app.exceptions.custom_exceptions import AppError
from app.utils.logger import get_logger

logger = get_logger(__name__)
_GO_BACK = frozenset({400, 409, 429})  # the person can fix it on the page they came from
_UNEXPECTED = "Something broke on our side. The run itself is unaffected: every lead is saved as it is found."


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _same_origin_referer(request: Request) -> str:
    referer = request.headers.get("referer", "")
    return referer if referer.startswith(str(request.base_url)) else ""


def _error(request: Request, status_code: int, detail: str) -> Response:
    if _wants_json(request):
        return JSONResponse({"detail": detail}, status_code=status_code)
    back = _same_origin_referer(request) if status_code in _GO_BACK else ""
    return render(request, "error.html", code=status_code, detail=detail, back=back, status_code=status_code)


def app_error_handler(request: Request, exc: AppError) -> Response:
    if exc.status_code >= 500:
        logger.error("%s at %s: %s", type(exc).__name__, request.url.path, exc.message)
    return _error(request, exc.status_code, exc.message)


def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    detail = "That page does not exist." if exc.status_code == 404 else str(exc.detail)
    return _error(request, exc.status_code, detail)


def validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    fields = sorted({str(e["loc"][-1]).replace("_", " ") for e in exc.errors() if e.get("loc")})
    return _error(
        request,
        400,
        f"Some of what was sent is not valid ({', '.join(fields) or 'input'}). Go back and check it.",
    )


def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    logger.exception("unhandled error at %s", request.url.path)
    return _error(request, 500, _UNEXPECTED)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
