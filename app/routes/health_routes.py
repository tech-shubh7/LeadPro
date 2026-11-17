from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config.database import engine

router = APIRouter(tags=["Health"])


@router.api_route("/healthz", methods=["GET", "HEAD"])
def healthz() -> JSONResponse:
    """Liveness plus a database round trip. Open even when the console is behind an access key."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        return JSONResponse({"ok": False, "database": type(exc).__name__}, status_code=503)
    return JSONResponse({"ok": True})
