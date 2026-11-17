"""Executes queued runs: claims them under a lease, heartbeats while working, survives crashes.

Embedded in the web server by default (one process for development and small deployments); the same
class runs standalone from `worker/run_worker.py` when runs should not share the web process.
"""

import asyncio
import contextlib
import os
import socket
from pathlib import Path

from app.config.settings import get_settings
from app.repositories import run_queue_repository as queue
from app.schemas.job_spec_schema import JobSpec
from app.services.run_service import run_job
from app.utils.logger import get_logger

logger = get_logger(__name__)
POLL_SECONDS = 5.0


class RunWorker:
    def __init__(self, concurrency: int | None = None, name: str | None = None):
        settings = get_settings()
        self.concurrency = concurrency or settings.worker_concurrency
        self.lease = settings.run_lease_seconds
        self.max_attempts = settings.run_max_attempts
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.out_dir = Path(settings.web_output_dir)
        self._running: dict[int, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._stopping = False

    @property
    def active(self) -> int:
        return sum(1 for t in self._running.values() if not t.done())

    def wake(self) -> None:
        """A run was just queued: claim it now rather than at the next poll."""
        self._wake.set()

    async def run_forever(self) -> None:
        logger.info("run worker %s started (up to %d runs at once)", self.name, self.concurrency)
        while not self._stopping:
            await self.tick()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)

    async def tick(self) -> None:
        """Claims runs up to capacity, and fails runs that ran out of attempts."""
        self._running = {job_id: t for job_id, t in self._running.items() if not t.done()}
        for job_id in await asyncio.to_thread(queue.give_up_exhausted, self.max_attempts):
            logger.error("run %s failed: no attempts left", job_id)
        while self.active < self.concurrency and not self._stopping:
            claimed = await asyncio.to_thread(queue.claim, self.name, self.lease, self.max_attempts)
            if claimed is None:
                return
            job_id, spec, attempt = claimed
            self._running[job_id] = asyncio.create_task(
                self._execute(job_id, spec, attempt), name=f"run:{job_id}"
            )

    async def _execute(self, job_id: int, spec: dict, attempt: int) -> None:
        if attempt > 1:
            logger.warning(
                "run %s: attempt %d after the previous worker stopped; starting it again", job_id, attempt
            )
            await asyncio.to_thread(queue.clear_partial_results, job_id)
        beat = asyncio.create_task(self._heartbeat(job_id))
        try:
            await run_job(JobSpec.model_validate(spec), self.out_dir, job_id=job_id)
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(queue.requeue, job_id, self.name))
            raise
        except Exception:  # run_job records its own failures; this is the last resort
            logger.exception("run %s failed outside the pipeline", job_id)
        finally:
            beat.cancel()
            await asyncio.shield(asyncio.to_thread(queue.release, job_id, self.name))

    async def _heartbeat(self, job_id: int) -> None:
        while True:
            await asyncio.sleep(self.lease / 4)
            if not await asyncio.to_thread(queue.heartbeat, job_id, self.name, self.lease):
                logger.error("run %s: lease lost to another worker", job_id)
                return

    async def stop(self, timeout_s: float = 10.0) -> None:
        """Graceful: running work is handed back to the queue without spending an attempt."""
        self._stopping = True
        self._wake.set()
        pending = [t for t in self._running.values() if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=timeout_s)
