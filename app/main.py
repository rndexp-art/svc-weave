"""FastAPI app entrypoint for the weave service.

Single weave container hosts:
  - the HTTP API (extraction, synthesis, generic CRUD, prompts, lifecycle, telegram-ingest)
  - two watcher tasks (extraction, synthesis)
  - two worker tasks (extraction, synthesis)

Singleton invariants are documented in AGENTS.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import bootstrap, graph, watcher, worker
from .config import settings
from .routes_admin import router as admin_router
from .routes_edges import router as edges_router
from .routes_extractions import router as extractions_router
from .routes_nodes import router as nodes_router
from .routes_prompts import router as prompts_router
from .routes_status import router as status_router
from .routes_syntheses import router as syntheses_router
from .routes_telegram import router as telegram_router


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)

log = logging.getLogger(__name__)


_BACKGROUND_TASKS: list[asyncio.Task] = []


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings()  # surface config errors loudly

    last_err: Exception | None = None
    for attempt in range(30):
        try:
            graph.init()
            graph.ensure_schema()
            bootstrap.bootstrap_service_agents()
            last_err = None
            break
        except Exception as e:
            last_err = e
            log.warning("neo4j not ready (attempt %d): %s", attempt + 1, e)
            await asyncio.sleep(2)
    if last_err is not None:
        log.error("neo4j init failed after retries; background tasks will keep trying: %s", last_err)

    _BACKGROUND_TASKS.append(asyncio.create_task(watcher.run_extraction(), name="extraction-watcher"))
    _BACKGROUND_TASKS.append(asyncio.create_task(watcher.run_synthesis(),  name="synthesis-watcher"))
    _BACKGROUND_TASKS.append(asyncio.create_task(worker.run_extraction(),  name="extraction-worker"))
    _BACKGROUND_TASKS.append(asyncio.create_task(worker.run_synthesis(),   name="synthesis-worker"))

    try:
        yield
    finally:
        for t in _BACKGROUND_TASKS:
            t.cancel()
        for t in _BACKGROUND_TASKS:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        graph.close()


app = FastAPI(
    title="rndexp-art weave",
    docs_url=None,
    redoc_url=None,
    lifespan=_lifespan,
)


app.include_router(status_router)
app.include_router(extractions_router)
app.include_router(syntheses_router)
app.include_router(nodes_router)
app.include_router(edges_router)
app.include_router(prompts_router)
app.include_router(admin_router)
app.include_router(telegram_router)
