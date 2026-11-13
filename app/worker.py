"""Run with `python -m app.worker` alongside the FastAPI service."""
import asyncio
import logging
import signal
from uuid import uuid4

from app.core.database import close_mongo_connection, connect_to_mongo
from app.services.job_refresh import JobRefreshService
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)


async def main():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    owner = uuid4().hex
    await connect_to_mongo()
    try:
        while not stop.is_set():
            operation = None
            stopping = None
            try:
                store = JobStore()
                await store.ensure_indexes()
                operation = asyncio.create_task(JobRefreshService(store).run_once(owner))
                stopping = asyncio.create_task(stop.wait())
                await asyncio.wait({operation, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set():
                    operation.cancel()
                await operation
            except asyncio.CancelledError:
                if not stop.is_set():
                    raise
            except Exception as exc:
                logger.error("Scheduler unavailable (%s). Retrying in 10 seconds.", type(exc).__name__)
            finally:
                tasks = [task for task in (operation, stopping) if task is not None]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass
    finally:
        await close_mongo_connection()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("primp").setLevel(logging.WARNING)
    logging.getLogger("ddgs").setLevel(logging.WARNING)
    asyncio.run(main())
