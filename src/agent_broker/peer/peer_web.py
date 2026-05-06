"""
--------------------------------------------------------------------------------
FILE:        peer_web.py
PATH:        ~/projects/agent-broker/src/agent_broker/peer/peer_web.py
DESCRIPTION: Operator web routes for peer transcript thread and message visibility.

CHANGELOG:
2026-05-06 16:15      Codex      [Fix] Explicitly bypass transcript recipient
                                      check only inside operator-authorized routes.
2026-05-06 13:40      Codex      [Fix] Preserve probe-toggle and pagination state across peer transcript navigation.
2026-05-06 13:02      Codex      [Feature] Add default pagination and explicit probe-visibility toggle for peer transcript UI.
2026-05-06 11:31      Codex      [Feature] Add Phase 7 peer transcript web routes behind the operator session.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import database as db
from ..config import DB_PATH
from ..routes.web import OP_TOKEN_COOKIE, _is_authed
from .services import DELIVERY_SERVICE

router = APIRouter(prefix="/web/peer")

_templates_dir = str(Path(__file__).resolve().parent.parent / "templates")
templates = Jinja2Templates(directory=_templates_dir)


def _conn():
    return db.get_connection(DB_PATH)


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/web/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/threads", response_class=HTMLResponse)
def threads_list(
    request: Request,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    include_probes: bool = Query(default=False),
    op_token: Optional[str] = Cookie(default=None, alias=OP_TOKEN_COOKIE),
):
    if not _is_authed(op_token):
        return _login_redirect()
    if limit < 1 or offset < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be >= 1 and offset must be >= 0",
        )
    conn = _conn()
    try:
        payload = DELIVERY_SERVICE.list_threads(
            conn,
            limit=limit,
            offset=offset,
            include_probes=include_probes,
        )
        return templates.TemplateResponse(
            request=request,
            name="peer_threads.html",
            context={
                "payload": payload,
                "subtitle": "Peer Transcript",
                "include_probes": include_probes,
                "limit": limit,
                "offset": offset,
            },
        )
    finally:
        conn.close()


@router.get("/threads/{thread_id}", response_class=HTMLResponse)
def thread_view(
    request: Request,
    thread_id: str,
    include_probes: bool = Query(default=False),
    op_token: Optional[str] = Cookie(default=None, alias=OP_TOKEN_COOKIE),
):
    if not _is_authed(op_token):
        return _login_redirect()
    conn = _conn()
    try:
        try:
            payload = DELIVERY_SERVICE.get_thread_transcript(
                conn,
                thread_id,
                include_probes=include_probes,
                # Operator auth owns transcript visibility here; agent
                # recipient authority remains enforced for non-operator calls.
                requires_recipient_check=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="peer_thread.html",
            context={
                "payload": payload,
                "subtitle": "Peer Transcript",
                "include_probes": include_probes,
            },
        )
    finally:
        conn.close()
