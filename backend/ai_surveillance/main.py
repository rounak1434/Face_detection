"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .captures import fail_stale_pending
from .config import Settings, get_settings
from .db import Database
from .deps import AppState
from .frigate_client import FrigateClient
from .media import MediaStore
from .routes import auth, cameras, captures, health, live
from .security import LoginThrottle

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, frigate_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_url)
        db.create_all()
        stale = fail_stale_pending(db.sessionmaker)
        if stale:
            log.info("Marked %d interrupted capture(s) as failed", stale)
        app.state.app = AppState(
            settings=settings,
            db=db,
            frigate=FrigateClient(settings, transport=frigate_transport),
            store=MediaStore(settings.media_root),
            throttle=LoginThrottle(settings.login_max_failures, settings.login_lockout_seconds),
        )
        yield
        state: AppState = app.state.app
        for task in list(state.background):
            task.cancel()
        await state.frigate.aclose()
        db.engine.dispose()

    app = FastAPI(title=f"{settings.app_display_name} API", lifespan=lifespan, docs_url=None, redoc_url=None)
    for module in (health, auth, cameras, captures, live):
        app.include_router(module.router)
    return app


def app() -> FastAPI:
    """Entry point for `uvicorn --factory ai_surveillance.main:app`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
