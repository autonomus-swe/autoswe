"""FastAPI app factory. The API never loads worker secrets (no require_worker call)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.routes import artifacts, control, events, health, runs
from core.settings import Settings, get_settings
from observability.logging import configure_logging, get_logger
from storage.db import make_engine
from storage.redis import RedisBus

log = get_logger(__name__)


async def build_arq_pool(settings: Settings) -> Any:
    from arq import create_pool
    from arq.connections import RedisSettings

    return await create_pool(RedisSettings.from_dsn(settings.redis_url))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = make_engine(settings.database_url, pool_size=5, max_overflow=2)
        app.state.bus = RedisBus(settings.redis_url)
        try:
            app.state.arq = await build_arq_pool(settings)
        except Exception as e:
            log.error("arq_pool_failed", error=f"{type(e).__name__}: {e}")
            app.state.arq = None
        try:
            yield
        finally:
            await app.state.bus.close()
            await app.state.engine.dispose()

    app = FastAPI(title="autoswe", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "unknown")
        log.error("unhandled_error", error=f"{type(exc).__name__}: {exc}", request_id=rid)
        return JSONResponse(
            status_code=500, content={"detail": "internal error", "request_id": rid}
        )

    # the run console: plain HTML, CSS and JavaScript, no build step
    static_dir = Path(__file__).parent / "static"
    app.mount("/ui", StaticFiles(directory=static_dir), name="ui")

    @app.get("/", include_in_schema=False)
    async def console() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    app.include_router(health.router)
    app.include_router(runs.router)
    app.include_router(events.router)
    app.include_router(control.router)
    app.include_router(artifacts.router)
    return app


def __getattr__(name: str) -> FastAPI:
    """``uvicorn api.main:app`` builds the app here, so importing this module for its
    factory (as tests do) never needs a configured environment."""
    if name == "app":
        return create_app()
    raise AttributeError(name)
