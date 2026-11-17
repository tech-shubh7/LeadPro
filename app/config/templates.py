"""Server-rendered pages: the Jinja environment and the one `render` every route uses."""

import hashlib
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from app.config.constants import COUNTRIES
from app.config.settings import get_settings
from app.services.signal_intelligence_service import INTENT_LABELS, SOURCE_TRUST
from app.utils.template_filters import FILTERS

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
CSRF_COOKIE = "lp_csrf"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters.update(FILTERS)
templates.env.trim_blocks = templates.env.lstrip_blocks = True
templates.env.globals.update(intent_labels=INTENT_LABELS, source_trust=SOURCE_TRUST)


def asset_version() -> str:
    """Content hash of the static bundle: static is cached forever, yet every deploy busts the cache."""
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def engine_info() -> dict:
    """What the model steps run on, shown in the sidebar so nobody demos on a dead key by surprise."""
    s = get_settings()
    if s.llm_mode == "gemini":
        return {"name": "Gemini", "model": s.gemini_judge_model, "ok": bool(s.gemini_api_key)}
    if s.llm_mode == "manual":
        return {"name": "Manual", "model": "answered by an operator", "ok": True}
    return {"name": "OpenAI", "model": s.llm_judge_model, "ok": bool(s.openai_api_key)}


def base_context(request: Request) -> dict:
    return {
        "v": request.app.state.asset_version,
        "engine": engine_info(),
        "csrf": getattr(request.state, "csrf", "") or request.cookies.get(CSRF_COOKIE, ""),
        "countries": COUNTRIES,
    }


def render(request: Request, name: str, status_code: int = 200, **context) -> Response:
    return templates.TemplateResponse(request, name, base_context(request) | context, status_code=status_code)
