"""
--------------------------------------------------------------------------------
FILE:        main.py
PATH:        C:/Projects/delphi-broker/src/delphi_broker/main.py
DESCRIPTION: FastAPI application entrypoint — mounts REST, MCP, and Web UI
             surfaces with shared SQLite backend.

CHANGELOG:
2026-03-31 17:30      Claude      [Header] Add file header
--------------------------------------------------------------------------------
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import database as db
from .config import DB_PATH, HOST, PORT
from .mcp_server import mcp
from .routes.api import router as api_router
from .routes.web import router as web_router


@asynccontextmanager
async def lifespan(application: FastAPI):
    conn = db.get_connection(DB_PATH)
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(title="Delphi Broker", version="0.2.0", lifespan=lifespan)

# Mount static files
_static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# REST API
app.include_router(api_router)

# Web UI
app.include_router(web_router)

# MCP server mounted at /mcp
mcp_app = mcp.streamable_http_app()
app.mount("/mcp", mcp_app)


def run():
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    run()
