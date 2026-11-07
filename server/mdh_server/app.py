"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import APIRouter, FastAPI

from mdh_server.api import errors, idempotency
from mdh_server.api.limits import BodySizeLimitMiddleware
from mdh_server.api.routes_access import router as access_router
from mdh_server.api.routes_admin import router as admin_router
from mdh_server.api.routes_alerts import router as alerts_router
from mdh_server.api.routes_auth import me_router
from mdh_server.api.routes_auth import router as auth_router
from mdh_server.api.routes_client import router as client_router
from mdh_server.api.routes_groups import router as groups_router
from mdh_server.api.routes_hosts import router as hosts_router
from mdh_server.api.routes_leases import router as leases_router
from mdh_server.api.routes_servers import router as servers_router
from mdh_server.api.routes_stats import router as stats_router
from mdh_server.api.routes_storage import router as storage_router
from mdh_server.api.ws import router as ws_router
from mdh_server.auth import tokens
from mdh_server.auth.passwords import PasswordService
from mdh_server.config import Settings
from mdh_server.context import AppContext
from mdh_server.coordination.service import Coordinator
from mdh_server.database.db import connect, immediate, migrate
from mdh_server.realtime import Hub
from mdh_server.relay import addresses
from mdh_server.relay.routes import router as relay_router
from mdh_server.relay.service import Relay
from mdh_server.alerts import RunMarker
from mdh_server import releases
from mdh_server.storage import retention
from mdh_server.storage.store import BlobStore
from mdh_shared import __version__
from mdh_shared.constants import MIN_CLIENT_VERSION

log = logging.getLogger("mdh.app")

HOUSEKEEPING_EVERY_S = 300.0


def build_context(settings: Settings, *, clock: Callable[[], float] = time.time,
                  password_hasher: PasswordHasher | None = None) -> AppContext:
    if settings.database_path != ":memory:":
        Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.database_path)
    migrate(conn)
    ctx = AppContext(settings=settings, conn=conn, passwords=PasswordService(password_hasher),
                     clock=clock, started_at=clock())
    ctx.hub = Hub()
    ctx.coordinator = Coordinator(ctx)
    ctx.store = BlobStore(Path(settings.storage_dir))
    ctx.releases = releases.shelves(Path(settings.updates_dir))
    ctx.run_marker = RunMarker(Path(settings.storage_dir).parent)
    ctx.relay = Relay(ctx)
    with immediate(conn):
        addresses.assign_missing(conn)
    return ctx


def housekeeping(ctx: AppContext) -> None:
    now = ctx.now()
    tokens.purge_expired(ctx.conn, now)
    with immediate(ctx.conn):
        idempotency.purge(ctx.conn, now)
        ctx.conn.execute("DELETE FROM offers WHERE status != 'pending' AND created_at < ?",
                         (now - 7 * 24 * 3600,))
    retention.collect(ctx.conn, ctx.store, now, ctx.settings.retention)


async def _sweeper(ctx: AppContext) -> None:
    last_housekeeping = 0.0
    while True:
        await asyncio.sleep(ctx.settings.sweep_interval_s)
        try:
            await ctx.coordinator.tick()
            if time.monotonic() - last_housekeeping > HOUSEKEEPING_EVERY_S:
                housekeeping(ctx)
                last_housekeeping = time.monotonic()
        except Exception:  # noqa: BLE001 - the sweeper must survive any single failure
            log.exception("coordinator sweep failed")


def create_app(settings: Settings | None = None, *, clock: Callable[[], float] = time.time,
               password_hasher: PasswordHasher | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx = build_context(settings, clock=clock, password_hasher=password_hasher)
        app.state.ctx = ctx
        log.info("control plane %s started (lease ttl=%ss grace=%ss)", __version__,
                 settings.lease.ttl_s, settings.lease.grace_s)
        ctx.run_marker.started(ctx.now())
        if ctx.run_marker.unclean_start_at is not None:
            log.warning("the previous run of the control plane did not stop cleanly")
        task = asyncio.create_task(_sweeper(ctx)) if settings.background_tasks else None
        if settings.relay_enabled:
            await ctx.relay.start()
        try:
            yield
        finally:
            await ctx.relay.stop()
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            with contextlib.suppress(Exception):  # statistics are never worth a failed shutdown
                ctx.coordinator.stats.flush(ctx.conn, ctx.now())
            ctx.conn.close()
            ctx.run_marker.stopped()

    docs = settings.expose_docs
    app = FastAPI(
        title="Minecraft Distributed Hosting",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/v1/docs" if docs else None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json" if docs else None,
    )
    errors.install(app)
    app.add_middleware(idempotency.IdempotencyMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)  # outermost: runs before anything reads the body

    api = APIRouter(prefix="/api/v1")

    @api.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "min_client_version": MIN_CLIENT_VERSION}

    for router in (auth_router, me_router, groups_router, hosts_router, servers_router,
                   leases_router, storage_router, ws_router, relay_router, client_router, admin_router,
                   alerts_router, access_router, stats_router):
        api.include_router(router)
    app.include_router(api)
    return app
