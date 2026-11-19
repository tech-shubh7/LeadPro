"""The operator console's FastAPI application: wiring only (lifespan, middleware, errors, routes).

Design choices, deliberately boring so they hold up in production:
- Server-rendered Jinja. No build step, no CDN, no client framework: a page works as soon as it arrives.
- Read endpoints are sync `def`, so Starlette runs them in its threadpool and blocking SQLAlchemy never
  blocks the event loop; endpoints that start background work are `async def` and hop to the threadpool
  explicitly for their database calls.
- Every list is paginated and every aggregate computed in SQL: page cost does not grow with the data.
- A run is reviewed before it starts: a wrong plan is the one error no downstream check can catch.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware

from app.config.settings import get_settings
from app.config.templates import STATIC_DIR, asset_version
from app.exceptions.handlers import register_exception_handlers
from app.middlewares.request_guard import request_guard
from app.providers.web.page_fetcher import prune_page_cache
from app.repositories import trace_repository
from app.routes.index import console_router
from app.services import run_manager_service
from app.services.run_worker_service import RunWorker
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def _recover_interrupted_work() -> None:
    """Work left 'in flight' by a previous process would otherwise show as running forever."""
    await run_in_threadpool(run_manager_service.sweep_orphans, True)
    if removed := await run_in_threadpool(prune_page_cache):
        logger.info("pruned %d expired cached page(s)", removed)
    if removed := await run_in_threadpool(trace_repository.prune_traces):
        logger.info("pruned %d expired model-call trace(s)", removed)
    for job_id in await run_in_threadpool(run_manager_service.orphaned_drafts):
        logger.warning("resuming the plan draft for job %s, interrupted by a restart", job_id)
        run_manager_service.start_draft(job_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await _recover_interrupted_work()
    except Exception:
        logger.exception("could not recover interrupted work at startup")
    worker, worker_task = None, None
    if get_settings().embedded_worker:  # runs execute here unless a standalone worker is used
        worker = RunWorker()
        run_manager_service.set_worker(worker)
        worker_task = asyncio.create_task(worker.run_forever(), name="run-worker")
    yield
    if worker is not None:
        await worker.stop()  # running work goes back to the queue, not lost
        worker_task.cancel()
        run_manager_service.set_worker(None)
    await run_manager_service.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="LeadPro", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.asset_version = asset_version()
    app.state.auth_token = settings.web_auth_token
    app.state.output_dir = Path(settings.web_output_dir).resolve()

    app.add_middleware(GZipMiddleware, minimum_size=700)
    app.middleware("http")(request_guard)
    register_exception_handlers(app)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(console_router)
    return app


app = create_app()
