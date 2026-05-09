"""
--------------------------------------------------------------------------------
FILE:        main.py
PATH:        ~/projects/agent-broker/src/agent_broker/main.py
DESCRIPTION: FastAPI application entrypoint wiring REST, web, MCP, and lifecycle services.

CHANGELOG:
2026-05-06 11:32      Codex      [Feature] Mount Phase 7 peer transcript web and REST operator routers.
2026-05-06 09:29      Codex      [Feature] Apply peer messaging schema migration during broker startup.
2026-05-06 08:30      Codex      [Refactor] Rename package to agent_broker and harden fail-loud Phase 1 broker boundaries.
--------------------------------------------------------------------------------

FastAPI application entrypoint for the v2 broker.

Mounts the operator REST surface at `/api/v1`, the phone-friendly web UI at
`/web`, the MCP server at `/mcp`, and serves static assets at `/static`.

A background task runs every 30s and skips any iteration whose nudge window
has expired (`workflow.auto_skip_expired_nudges`). The task is started in
the lifespan startup hook and cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import database as db
from . import workflow
from .config import (
    DB_PATH,
    HOST,
    MCP_ORIGIN_REGISTRY,
    MCP_SESSION_MANAGER_ENABLED,
    NUDGE_SWEEP_ENABLED,
    PORT,
)
from .collaboration import collab_store
from .collaboration.collab_api import router as collab_api_router
from .collaboration.collab_web import router as collab_web_router
from .mcp_server import mcp
from .peer import peer_store
from .peer.peer_api import router as peer_api_router
from .peer.peer_web import router as peer_web_router
from .routes.api import router as api_router
from .routes.web import router as web_router
from .transport_policy import TransportPolicy, validate_origin
from .v3 import database as v3db
from .v3.api import router as v3_api_router
from .v3.web import router as v3_web_router

logger = logging.getLogger(__name__)

NUDGE_SWEEP_INTERVAL_SECS = 30


async def _nudge_sweep_loop() -> None:
    """Periodically auto-skip iterations whose nudge windows have expired."""
    while True:
        try:
            await asyncio.sleep(NUDGE_SWEEP_INTERVAL_SECS)
            conn = db.get_connection(DB_PATH)
            try:
                count = workflow.auto_skip_expired_nudges(conn)
                if count:
                    logger.info("auto-skipped %d expired nudge(s)", count)
            finally:
                conn.close()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — log and keep running
            logger.exception("nudge sweep iteration failed")


# MCP server (JSON-RPC over HTTP). Build the sub-app once so its session
# manager can be started inside our lifespan.
mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Ensure DB is initialised once at startup. v2 schema first, then v3
    # tables alongside (idempotent).
    conn = db.get_connection(DB_PATH)
    db.init_db(conn)
    v3db.init_v3_schema(conn)
    peer_store.init_peer_schema(conn)
    collab_store.init_collab_schema(conn)
    conn.close()
    # Start the FastMCP session manager when MCP transport is enabled.
    # Direct unit tests call tool functions without the stream manager.
    mcp_context = mcp.session_manager.run() if MCP_SESSION_MANAGER_ENABLED else nullcontext()
    async with mcp_context:
        sweep_task = (
            asyncio.create_task(_nudge_sweep_loop(), name="nudge-sweep")
            if NUDGE_SWEEP_ENABLED
            else None
        )
        try:
            yield
        finally:
            if sweep_task is not None:
                sweep_task.cancel()
                try:
                    await sweep_task
                except asyncio.CancelledError:
                    pass


app = FastAPI(title="Agent Broker", version="0.2.0", lifespan=lifespan)
_transport_policy = TransportPolicy(
    origin_registry=MCP_ORIGIN_REGISTRY,
)


@app.middleware("http")
async def enforce_origin_policy(request, call_next):
    if request.url.path.startswith("/mcp"):
        return await call_next(request)
    client = request.client.host if request.client else None
    allowed, reason = validate_origin(
        policy=_transport_policy,
        client_host=client,
        origin=request.headers.get("origin"),
    )
    if not allowed:
        return JSONResponse(
            status_code=403,
            content={"error": "origin_rejected", "reason": reason},
        )
    return await call_next(request)


# Static assets.
_static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# REST API + web UI.
app.include_router(api_router)
app.include_router(web_router)
app.include_router(peer_api_router)
app.include_router(peer_web_router)
app.include_router(collab_api_router)
app.include_router(collab_web_router)
app.include_router(v3_api_router)  # /api/v2/* — v3 task lifecycle
app.include_router(v3_web_router)  # /web/v3/* — operator UI for v3 tasks

# Mount the MCP sub-app. streamable_http_app exposes its routes at /mcp
# internally, so mounting at "" keeps the public path /mcp (rather than
# the original buggy /mcp/mcp double-mount).
app.mount("", mcp_app)


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    run()
