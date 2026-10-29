"""FastAPI application factory."""

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .captures import fail_stale_pending, retention_loop
from .config import Settings, get_settings
from .credentials import CredentialCipher, load_key
from .db import Database
from .deps import AppState
from .frigate_client import FrigateClient
from .media import MediaStore
from .inference_client import InferenceClient
from .routes import analysis, auth, cameras, captures, devices, health, live
from .security import LoginThrottle, purge_expired_sessions

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, frigate_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_url)
        db.migrate()
        stale = fail_stale_pending(db.sessionmaker)
        if stale:
            log.info("Marked %d interrupted capture(s) as failed", stale)
        with db.sessionmaker() as session:
            purge_expired_sessions(session)
        store = MediaStore(settings.media_root)
        partials = store.remove_partials()
        if partials:
            log.info("Removed %d partial capture file(s) left by a restart", partials)
        app.state.app = AppState(
            settings=settings,
            db=db,
            frigate=FrigateClient(settings, transport=frigate_transport),
            store=store,
            throttle=LoginThrottle(settings.login_max_failures, settings.login_lockout_seconds),
            cipher=CredentialCipher(load_key(settings)),
            inference=InferenceClient(settings),
            analysis_slots=asyncio.Semaphore(settings.analysis_concurrency),
        )
        if settings.capture_retention_days > 0:
            app.state.app.spawn(retention_loop(db.sessionmaker, store, settings.capture_retention_days))
        yield
        state: AppState = app.state.app
        for task in list(state.background):
            task.cancel()
        await state.frigate.aclose()
        await state.inference.aclose()
        db.engine.dispose()

    app = FastAPI(title=f"{settings.app_display_name} API", lifespan=lifespan, docs_url=None, redoc_url=None)
    for module in (health, auth, cameras, captures, devices, analysis, live):
        app.include_router(module.router)
    return app


def app() -> FastAPI:
    """Entry point for `uvicorn --factory ai_surveillance.main:app`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
