"""FastAPI application.

Run in production with:  uvicorn control_api.main:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from control_api import __version__, provisioning
from control_api.config import get_settings
from control_api.db import init_db
from control_api.routers import internal

log = logging.getLogger(__name__)


def _start_worker(stop: threading.Event) -> threading.Thread:
    """Background sweeper for provisioning jobs.

    Its only job is to un-strand work: a job whose BackgroundTask died with the
    process, or one waiting out a retry backoff. Everything it does is idempotent,
    so running it alongside explicit `advance` calls is safe.

    (Single-replica assumption. With >1 control-api replica two workers could pick
    up the same job; the steps tolerate it, but Phase 1 should add a row lock.)
    """
    interval = get_settings().provision_worker_interval_seconds

    def loop() -> None:
        while not stop.wait(interval):
            try:
                provisioning.run_pending_jobs()
            except Exception:  # noqa: BLE001 -- the worker must never die
                log.exception("provisioning worker tick failed")

    thread = threading.Thread(target=loop, name="provision-worker", daemon=True)
    thread.start()
    return thread


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        init_db()
        stop = threading.Event()
        thread = _start_worker(stop) if settings.provision_worker_enabled else None
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=5)

    app = FastAPI(
        title="Squire control-api",
        version=__version__,
        description=(
            "Tenant registry, Telegram bot pool, and Railway-driven provisioning. "
            "Holds no conversation content and no plaintext user credentials."
        ),
        lifespan=lifespan,
    )
    app.include_router(internal.router)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        """Unauthenticated liveness probe for Railway."""
        return {"status": "ok", "version": __version__}

    return app


app = create_app()


def run() -> None:  # pragma: no cover -- console-script entrypoint
    import os

    import uvicorn

    uvicorn.run(
        "control_api.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )
