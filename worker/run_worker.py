"""Standalone run worker: `python -m worker.run_worker` (or `just worker`).

Use it when runs should not share the web process: set EMBEDDED_WORKER=false for the web server and run
one or more of these. Any number can run at once; the queue guarantees each run has one owner.
"""

import asyncio
import signal

from app.config.settings import get_settings
from app.services.run_worker_service import RunWorker
from app.utils.logger import configure_logging


async def main() -> None:
    worker = RunWorker()
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    runner = asyncio.create_task(worker.run_forever())
    await stopped.wait()
    await worker.stop()
    runner.cancel()


if __name__ == "__main__":
    configure_logging(get_settings().log_level)
    asyncio.run(main())
