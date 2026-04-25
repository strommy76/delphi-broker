"""FastAPI application entrypoint for the v2 broker.

Mounts the operator REST surface at `/api/v1`, the phone-friendly web UI at
`/web`, the MCP server at `/mcp`, and serves static assets at `/static`.

A background task runs every 30s and skips any iteration whose nudge window
has expired (`workflow.auto_skip_expired_nudges`). The task is started in
the lifespan startup hook and cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import database as db
from . import workflow
from .config import DB_PATH, HOST, PORT
from .mcp_server import mcp
from .routes.api import router as api_router
from .routes.web import router as web_router

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


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Ensure DB is initialised once at startup.
    conn = db.get_connection(DB_PATH)
    db.init_db(conn)
    conn.close()
    # Spawn the background sweep task.
    sweep_task = asyncio.create_task(_nudge_sweep_loop(), name="nudge-sweep")
    try:
        yield
    finally:
        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Delphi Broker", version="0.2.0", lifespan=lifespan)

# Static assets.
_static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# REST API + web UI.
app.include_router(api_router)
app.include_router(web_router)

# MCP server (JSON-RPC over HTTP).
mcp_app = mcp.streamable_http_app()
app.mount("/mcp", mcp_app)


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    run()
