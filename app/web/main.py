"""Operator console: start a run from a website and an ask, watch it work, inspect every piece of
evidence behind every lead, and action an opt-out or erasure.

Design choices, deliberately boring so they hold up in production:
- Server-rendered Jinja. No build step, no CDN, no client framework: a page is interactive as soon as
  it arrives, and there is no JavaScript bundle to break.
- Read endpoints are sync `def`, so Starlette runs them in its threadpool and blocking SQLAlchemy never
  blocks the event loop. The two endpoints that start background work are `async def` and push their
  database calls to the threadpool explicitly.
- Every list is paginated and every aggregate is computed in SQL (queries.py): page cost does not grow
  with the size of the database.
- A run is reviewed before it is started. The quote, date and email checks downstream cannot catch a
  wrong ICP, so the drafted plan is always shown first, with the cost cap, before anything is spent.
"""
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models.enums import JobStatus
from app.pipeline import store
from app.pipeline.intake import COUNTRIES, Plan
from app.pipeline.jobspec import MAX_COST_CAP_INR
from app.web import queries as q
from app.web import runs
from app.web.filters import FILTERS

log = logging.getLogger("leadpro.web")
HERE = Path(__file__).parent
AUTH_COOKIE = "lp_auth"
CSRF_COOKIE = "lp_csrf"
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
STRENGTHS = ("very_high", "high", "medium", "low")


def _asset_version() -> str:
    """Content hash of the static bundle: lets static be cached forever and still update on deploy."""
    digest = hashlib.sha256()
    for path in sorted((HERE / "static").rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters.update(FILTERS)
templates.env.trim_blocks = templates.env.lstrip_blocks = True


def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


Db = Annotated[Session, Depends(db)]


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:  # work left "in flight" by a previous process would otherwise spin forever
            await run_in_threadpool(runs.sweep_orphans)
            await run_in_threadpool(_prune_page_cache)
            for job_id in await run_in_threadpool(runs.orphaned_drafts):
                log.warning("resuming the plan draft for job %s, interrupted by a restart", job_id)
                runs.start_draft(job_id)
        except Exception:
            log.exception("could not recover interrupted work at startup")
        yield
        await runs.shutdown()

    app = FastAPI(title="LeadPro", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.asset_version = _asset_version()
    app.state.auth_token = settings.web_auth_token
    app.state.output_dir = Path(settings.web_output_dir).resolve()
    app.add_middleware(GZipMiddleware, minimum_size=700)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        token = request.app.state.auth_token
        path = request.url.path
        # The CSRF token must exist while the first page renders, not only after its response sets the
        # cookie, or a first-time visitor's first form post is rejected as stale.
        new_csrf = None if request.cookies.get(CSRF_COOKIE) else secrets.token_urlsafe(32)
        request.state.csrf = request.cookies.get(CSRF_COOKIE) or new_csrf
        if token and not path.startswith(("/static/", "/healthz")):
            query_token = request.query_params.get("token", "")
            cookie_ok = secrets.compare_digest(request.cookies.get(AUTH_COOKIE, ""), _session_value(token))
            if path == "/signin" and request.method == "POST":
                who = request.client.host if request.client else "?"
                if _signin_throttled(who):
                    return templates.TemplateResponse(request, "signin.html", _base_ctx(request) | {
                        "bad": True, "throttled": True, "next": "/"}, status_code=429)
                form = await request.form()
                ok = secrets.compare_digest(str(form.get("token", "")), token)
                nxt = _local_path(str(form.get("next") or "/"))
                if not ok:
                    _signin_failed(who)
                    return templates.TemplateResponse(request, "signin.html", _base_ctx(request) | {
                        "bad": True, "next": nxt}, status_code=401)
                resp = RedirectResponse(nxt, status_code=HTTP_303_SEE_OTHER)
                resp.set_cookie(AUTH_COOKIE, _session_value(token), httponly=True, samesite="lax",
                                secure=request.url.scheme == "https", max_age=30 * 86400)
                return resp
            if not cookie_ok and not (query_token and secrets.compare_digest(query_token, token)):
                nxt = path + (f"?{request.url.query}" if request.url.query else "")
                return templates.TemplateResponse(request, "signin.html", _base_ctx(request) | {
                    "next": nxt if request.method == "GET" else "/"}, status_code=401)
            if query_token and secrets.compare_digest(query_token, token):  # sign in once, then keep a cookie
                resp = RedirectResponse(str(request.url.remove_query_params("token")), status_code=HTTP_303_SEE_OTHER)
                resp.set_cookie(AUTH_COOKIE, _session_value(token), httponly=True, samesite="lax",
                                secure=request.url.scheme == "https", max_age=30 * 86400)
                return resp
        response = await call_next(request)
        response.headers.update({
            "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            "Cross-Origin-Opener-Policy": "same-origin",
        })
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-store"  # every page holds personal data
            if new_csrf:
                response.set_cookie(CSRF_COOKIE, new_csrf, httponly=False, samesite="strict",
                                    secure=request.url.scheme == "https")
        return response

    _routes(app)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """Every refusal as a page a person can act on, never a raw JSON body. Polling endpoints
        (Accept: application/json) still get JSON."""
        detail = exc.detail if exc.status_code != 404 else "That page does not exist."
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": detail}, status_code=exc.status_code)
        return _render(request, "error.html", code=exc.status_code, detail=detail, status_code=exc.status_code,
                       back=_same_origin_referer(request) if exc.status_code in (400, 409, 429) else "")

    @app.exception_handler(RequestValidationError)
    def bad_input(request: Request, exc: RequestValidationError) -> Response:
        fields = sorted({str(e["loc"][-1]).replace("_", " ") for e in exc.errors() if e.get("loc")})
        detail = f"Some of what was sent is not valid ({', '.join(fields) or 'input'}). Go back and check it."
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": detail}, status_code=400)
        return _render(request, "error.html", code=400, detail=detail, status_code=400,
                       back=_same_origin_referer(request))

    @app.exception_handler(500)
    @app.exception_handler(Exception)
    def failed(request: Request, exc: Exception) -> Response:
        log.exception("console error at %s", request.url.path)
        return _render(request, "error.html", code=500,
                       detail="Something broke on our side. The run itself is unaffected: "
                              "every lead is saved as it is found.", status_code=500)

    return app


def _engine() -> dict:
    """What the LLM steps run on, shown in the sidebar so nobody demos a run on a dead key by surprise."""
    s = get_settings()
    if s.llm_mode == "gemini":
        return {"name": "Gemini", "model": s.gemini_judge_model, "ok": bool(s.gemini_api_key)}
    if s.llm_mode == "manual":
        return {"name": "Manual", "model": "answered by an operator", "ok": True}
    return {"name": "OpenAI", "model": s.llm_judge_model, "ok": bool(s.openai_api_key)}


def _base_ctx(request: Request) -> dict:
    return {"v": request.app.state.asset_version, "engine": _engine(),
            "csrf": getattr(request.state, "csrf", "") or request.cookies.get(CSRF_COOKIE, ""),
            "countries": COUNTRIES}


def _render(request: Request, name: str, status_code: int = 200, **ctx) -> Response:
    return templates.TemplateResponse(request, name, _base_ctx(request) | ctx, status_code=status_code)


SIGNIN_WINDOW_S = 900
SIGNIN_MAX_FAILURES = 10
_signin_failures: dict[str, list[float]] = {}


def _signin_throttled(who: str) -> bool:
    """At most SIGNIN_MAX_FAILURES wrong keys per address per window: an access key cannot be guessed
    by brute force. In-process, which matches the single-process deployment."""
    now = time.monotonic()
    if len(_signin_failures) > 10_000:  # many addresses: drop the ones whose window has passed
        for key in [k for k, ts in _signin_failures.items() if not ts or now - ts[-1] >= SIGNIN_WINDOW_S]:
            del _signin_failures[key]
    recent = [t for t in _signin_failures.get(who, []) if now - t < SIGNIN_WINDOW_S]
    _signin_failures[who] = recent
    return len(recent) >= SIGNIN_MAX_FAILURES


def _signin_failed(who: str) -> None:
    _signin_failures.setdefault(who, []).append(time.monotonic())


def _local_path(target: str) -> str:
    """Only a path on this site: '//evil.com' and '/\\evil.com' are other sites to a browser."""
    return target if target.startswith("/") and not target.startswith(("//", "/\\")) else "/"


def _prune_page_cache() -> None:
    from datetime import timedelta

    from app.core.fetcher import PageCache

    settings = get_settings()
    # Development keeps pages for free reruns, but not forever; production keeps them for the cache TTL.
    keep = timedelta(days=30) if settings.is_dev else timedelta(hours=settings.page_cache_ttl_hours)
    removed = PageCache(Path("cache/pages"), None).prune(keep)
    if removed:
        log.info("pruned %d cached page(s) older than %s", removed, keep)


def _session_value(token: str) -> str:
    """What the sign-in cookie holds: a keyed hash of the access key, never the key itself, so a copied
    cookie or a log line does not reveal the key (and rotating the key signs everyone out)."""
    return hmac.new(token.encode(), b"leadpro-console-session-v1", hashlib.sha256).hexdigest()


def _same_origin_referer(request: Request) -> str:
    ref = request.headers.get("referer", "")
    return ref if ref.startswith(str(request.base_url)) else ""


def _check_csrf(request: Request, csrf: str) -> None:
    """Double-submit cookie: a cross-site form post cannot read the cookie to copy it into the body."""
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not cookie or not csrf or not secrets.compare_digest(cookie, csrf):
        raise HTTPException(status_code=400, detail="This form went stale. Reload the page and try again.")


def _lines(value: str) -> list[str]:
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def _commas(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _routes(app: FastAPI) -> None:
    # --- health and dashboard -------------------------------------------------------------------
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz() -> JSONResponse:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse({"ok": False, "database": type(exc).__name__}, status_code=503)
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, session: Db, page: int = Query(1, ge=1), q_: str = Query("", alias="q"),
                  status: str = "") -> Response:
        runs.sweep_orphans()
        status = status if status in set(JobStatus) else ""
        return _render(request, "dashboard.html", totals=q.totals(session), nav="runs",
                       jobs=q.jobs_page(session, page, q_, status), q=q_, status=status,
                       statuses=[s.value for s in JobStatus])

    # --- new run: ask -> drafted plan -> review -> start ----------------------------------------
    @app.get("/new", response_class=HTMLResponse)
    def new_run(request: Request) -> Response:
        settings = get_settings()
        return _render(request, "new.html", nav="new", default_cap=settings.max_cost_per_job_inr,
                       default_freshness=settings.freshness_days, form={}, errors={})

    @app.post("/new")
    async def create_run(request: Request, website: Annotated[str, Form()] = "",
                         ask: Annotated[str, Form()] = "", count: Annotated[int, Form()] = 25,
                         country: Annotated[str, Form()] = "GB", client_name: Annotated[str, Form()] = "",
                         freshness_days: Annotated[int, Form()] = 90,
                         personalized_email: Annotated[bool, Form()] = False,
                         include_icp_only: Annotated[bool, Form()] = False,
                         cost_cap_inr: Annotated[float, Form()] = 0.0,
                         csrf: Annotated[str, Form()] = "") -> Response:
        """Validates the ask, then drafts the plan in the background."""
        _check_csrf(request, csrf)
        settings = get_settings()
        from app.core.normalize import normalize_domain

        form = {"website": website, "ask": ask, "count": count, "country": country,
                "client_name": client_name, "freshness_days": freshness_days,
                "personalized_email": personalized_email, "include_icp_only": include_icp_only,
                "cost_cap_inr": cost_cap_inr or settings.max_cost_per_job_inr}
        errors: dict[str, str] = {}
        if not normalize_domain(website):
            errors["website"] = "Enter the client's website, e.g. acme.co.uk — the plan is drafted from it."
        if len(ask.strip()) < 10:
            errors["ask"] = "Describe who to find, e.g. 'UK 3PLs hiring warehouse staff'."
        if not 1 <= count <= 500:
            errors["count"] = "Between 1 and 500."
        if not 1 <= freshness_days <= 365:
            errors["freshness_days"] = "Between 1 and 365 days."
        if not 0 < form["cost_cap_inr"] <= MAX_COST_CAP_INR:
            errors["cost_cap_inr"] = f"Between ₹1 and ₹{MAX_COST_CAP_INR:,.0f}."
        if country.upper() not in COUNTRIES:
            errors["country"] = "Pick a country from the list."
        if errors:
            return _render(request, "new.html", nav="new", form=form, errors=errors,
                           default_cap=settings.max_cost_per_job_inr,
                           default_freshness=settings.freshness_days, status_code=400)

        if runs.active_drafts() >= runs.MAX_CONCURRENT_DRAFTS:
            errors["website"] = "A few plans are already being drafted. Try again in a minute."
            return _render(request, "new.html", nav="new", form=form, errors=errors,
                           default_cap=settings.max_cost_per_job_inr,
                           default_freshness=settings.freshness_days, status_code=429)
        job_id = await run_in_threadpool(
            runs.create_draft_job, website.strip(), ask.strip(), count, country.upper(),
            client_name.strip(), freshness_days, personalized_email, include_icp_only, form["cost_cap_inr"])
        runs.start_draft(job_id)
        return RedirectResponse(f"/new/{job_id}", status_code=HTTP_303_SEE_OTHER)

    @app.get("/new/{job_id}", response_class=HTMLResponse)
    def review(request: Request, session: Db, job_id: int) -> Response:
        job = session.get(q.Job, job_id)
        if job is None:
            raise HTTPException(404)
        if job.status == JobStatus.RESEARCHING_CLIENT:
            return _render(request, "drafting.html", nav="new", job=job)
        if job.status != JobStatus.AWAITING_ICP_REVIEW:
            return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)
        return _render(request, "review.html", nav="new", job=job, plan=runs.plan_for_review(job),
                       strengths=STRENGTHS, busy=runs.active_runs() >= runs.MAX_CONCURRENT_RUNS,
                       max_runs=runs.MAX_CONCURRENT_RUNS)

    @app.get("/new/{job_id}/status")
    def draft_status(session: Db, job_id: int) -> JSONResponse:
        row = session.execute(text("SELECT status, error FROM jobs WHERE id = :id"), {"id": job_id}).first()
        if row is None:
            raise HTTPException(404)
        return JSONResponse({"status": row[0], "ready": row[0] != JobStatus.RESEARCHING_CLIENT.value,
                             "error": row[1]})

    @app.post("/new/{job_id}/redraft")
    async def redraft(request: Request, job_id: int, csrf: Annotated[str, Form()] = "") -> Response:
        """Drafts the plan again, e.g. after the model was overloaded and a generic template came back."""
        _check_csrf(request, csrf)
        moved = await run_in_threadpool(runs.reopen_draft, job_id)
        if moved:
            runs.start_draft(job_id)
        return RedirectResponse(f"/new/{job_id}", status_code=HTTP_303_SEE_OTHER)

    @app.post("/new/{job_id}/start")
    async def start(request: Request, job_id: int,
                    ask: Annotated[str, Form()] = "", count: Annotated[int, Form()] = 25,
                    freshness_days: Annotated[int, Form()] = 90, cost_cap_inr: Annotated[float, Form()] = 0.0,
                    personalized_email: Annotated[bool, Form()] = False,
                    include_icp_only: Annotated[bool, Form()] = False,
                    industries: Annotated[str, Form()] = "", geographies: Annotated[str, Form()] = "",
                    size_band: Annotated[str, Form()] = "", business_model: Annotated[str, Form()] = "",
                    why_they_buy: Annotated[str, Form()] = "", disqualifiers: Annotated[str, Form()] = "",
                    buyer_titles: Annotated[str, Form()] = "", negative: Annotated[str, Form()] = "",
                    offer: Annotated[str, Form()] = "", why_choose: Annotated[str, Form()] = "",
                    signal_type: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI needs a default
                    signal_description: Annotated[list[str], Form()] = [],  # noqa: B006
                    signal_strength: Annotated[list[str], Form()] = [],  # noqa: B006
                    signal_kind: Annotated[list[str], Form()] = [],  # noqa: B006
                    signal_queries: Annotated[list[str], Form()] = [],  # noqa: B006
                    csrf: Annotated[str, Form()] = "") -> Response:
        """Takes the reviewed plan (as edited), builds the job spec and starts the run."""
        _check_csrf(request, csrf)
        job = await run_in_threadpool(_load_job, job_id)
        if job is None:
            raise HTTPException(404)
        if runs.is_busy(job_id) or job.status != JobStatus.AWAITING_ICP_REVIEW:
            # Already started (a double-click, the back button): show the run instead of a second copy.
            return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)
        if runs.active_runs() >= runs.MAX_CONCURRENT_RUNS:
            raise HTTPException(status_code=429,
                                detail=f"{runs.MAX_CONCURRENT_RUNS} runs are already going. Wait for one to finish.")

        signals = []
        for i, kind_ in enumerate(signal_type):
            if not kind_.strip():
                continue  # cleared by the operator: dropped
            at = lambda seq, default="": seq[i].strip() if i < len(seq) else default  # noqa: B023
            queries = _lines(signal_queries[i] if i < len(signal_queries) else "")
            if not queries:
                continue  # a signal with no query cannot be searched for
            signals.append({"type": kind_.strip(), "description": at(signal_description) or kind_.strip(),
                            "strength": at(signal_strength) if at(signal_strength) in STRENGTHS else "medium",
                            "kind": "news" if at(signal_kind) == "news" else "search", "queries": queries})

        plan = Plan(
            ask=ask.strip() or job.ask, count=count, country=(job.target_countries or ["GB"])[0],
            client_name=job.client_name, client_website=job.client_website,
            client_summary=(job.icp or {}).get("intake", {}).get("client_summary", ""),
            offer=offer.strip() or ask.strip(),
            icp={"industries": _commas(industries), "geographies": _commas(geographies),
                 "size_band": size_band.strip(), "business_model": business_model.strip(),
                 "why_they_buy": _lines(why_they_buy), "disqualifiers": _lines(disqualifiers)},
            signals=signals, buyer_titles=_commas(buyer_titles), negative=_lines(negative),
            sources=((job.icp or {}).get("intake", {}).get("sources") or []),
            why_choose=_lines(why_choose), freshness_days=freshness_days,
            personalized_email=personalized_email, include_icp_only=include_icp_only,
            cost_cap_inr=cost_cap_inr or job.cost_cap_inr,
        )
        try:
            spec = plan.to_spec(job.client_key)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise HTTPException(status_code=400,
                                detail=f"The plan is not runnable: {first['loc'][-1]} — {first['msg']}") from exc
        if not spec.playbook.signals:
            raise HTTPException(status_code=400, detail="Keep at least one signal with a search query.")
        if not spec.playbook.buyer_titles:
            raise HTTPException(status_code=400, detail="Name at least one buyer title to look for.")

        runs.start_run(job_id, spec)
        return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)

    # --- a run ----------------------------------------------------------------------------------
    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, session: Db, job_id: int, page: int = Query(1, ge=1), tier: str = "",
                   contact: str = "", q_: str = Query("", alias="q"), tab: str = "leads") -> Response:
        job = session.get(q.Job, job_id)
        if job is None:
            raise HTTPException(404)
        if job.status in (JobStatus.RESEARCHING_CLIENT, JobStatus.AWAITING_ICP_REVIEW):
            return RedirectResponse(f"/new/{job_id}", status_code=HTTP_303_SEE_OTHER)
        tab = tab if tab in ("leads", "rejected", "method") else "leads"
        return _render(request, "job.html", job=job, nav="runs", tab=tab,
                       leads=q.job_leads_page(session, job_id, page, tier, contact, q_) if tab == "leads" else None,
                       rejected=q.job_rejections_page(session, job_id, page) if tab == "rejected" else None,
                       counts=q.lead_counts_for_jobs(session, [job_id]).get(job_id, q.LeadCounts()),
                       reasons=q.rejection_reasons(session, job), costs=q.cost_breakdown(job),
                       files=_job_files(app, job), tier=tier, contact=contact, q=q_,
                       plan=runs.plan_for_review(job), running=job.status in q.RUNNING_STATUSES)

    @app.get("/jobs/{job_id}/progress")
    def progress(session: Db, job_id: int) -> JSONResponse:
        data = q.job_progress(session, job_id)
        if data is None:
            raise HTTPException(404)
        return JSONResponse(data)

    @app.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead(request: Request, session: Db, lead_id: int) -> Response:
        detail = q.lead_detail(session, lead_id)
        if detail is None:
            raise HTTPException(404)
        return _render(request, "lead.html", d=detail, nav="runs")

    @app.get("/jobs/{job_id}/files/{name}")
    def download(request: Request, session: Db, job_id: int, name: str) -> FileResponse:
        """Only files this job actually produced, matched by name against the job's own stem: a path
        from the URL is never joined onto the output directory."""
        job = session.get(q.Job, job_id)
        if job is None:
            raise HTTPException(404)
        match = next((p for p in _job_files(request.app, job) if p.name == name), None)
        if match is None:
            raise HTTPException(404)
        return FileResponse(match, filename=match.name)

    # --- suppression and erasure ----------------------------------------------------------------
    @app.get("/suppressions", response_class=HTMLResponse)
    def suppressions(request: Request, session: Db, page: int = Query(1, ge=1), q_: str = Query("", alias="q"),
                     done: str = "") -> Response:
        return _render(request, "suppressions.html", nav="suppressions", done=done, q=q_,
                       rows=q.suppressions_page(session, page, q_))

    @app.post("/suppressions")
    def add_suppression(request: Request, session: Db, kind: Annotated[str, Form()],
                        value: Annotated[str, Form()], reason: Annotated[str, Form()],
                        csrf: Annotated[str, Form()] = "") -> Response:
        _check_csrf(request, csrf)
        if kind not in ("domain", "email") or not value.strip() or not reason.strip():
            raise HTTPException(status_code=400, detail="Pick domain or email, and give a value and a reason.")
        store.add_suppression(session, kind, value, reason.strip()[:300])
        session.commit()
        return RedirectResponse(f"/suppressions?done=Suppressed+this+{kind}.", status_code=HTTP_303_SEE_OTHER)

    @app.post("/suppressions/erase")
    def erase(request: Request, session: Db, email: Annotated[str, Form()], reason: Annotated[str, Form()],
              csrf: Annotated[str, Form()] = "") -> Response:
        """UK GDPR / DPDP erasure: deletes the stored contact rows and suppresses the address."""
        _check_csrf(request, csrf)
        if "@" not in email or not reason.strip():
            raise HTTPException(status_code=400, detail="Give a valid email address and a reason.")
        n = store.erase_person_by_email(session, email, reason.strip()[:300])
        session.commit()
        return RedirectResponse(f"/suppressions?done=Erased+{n}+contact+record(s)+and+suppressed+the+address.",
                                status_code=HTTP_303_SEE_OTHER)


def _load_job(job_id: int):
    with SessionLocal() as session:
        return session.get(q.Job, job_id)


def _job_files(app: FastAPI, job) -> list[Path]:
    """Exports this job wrote, found by the stem the exporter uses (client key + job id)."""
    out = app.state.output_dir
    if not out.is_dir():
        return []
    return sorted(p for p in out.glob(f"{job.client_key}_job{job.id}_*") if p.is_file())


app = create_app()
