"""Phone-friendly web UI for the v2 broker.

Cookie-based auth: a single operator token (the same one used by the REST
header `X-Operator-Token`) is verified at `/web/login`. On success an
HTTP-only cookie is set; subsequent requests reuse it.

Routes are deliberately minimal — enough to nudge, abort, resolve
escalations, and read the transcript from a phone. Templates live in
`templates/` and reuse `base.html` / `style.css`.
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import database as db
from .. import workflow
from ..config import DB_PATH, WEB_SECURE, require_operator_token

router = APIRouter(prefix="/web")

_templates_dir = str(Path(__file__).resolve().parent.parent / "templates")
templates = Jinja2Templates(directory=_templates_dir)

OP_TOKEN_COOKIE = "op_token"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    return db.get_connection(DB_PATH)


def _is_authed(op_token: Optional[str]) -> bool:
    if not op_token:
        return False
    try:
        expected = require_operator_token()
    except RuntimeError:
        return False
    return secrets.compare_digest(op_token, expected)


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/web/login", status_code=status.HTTP_303_SEE_OTHER)


def _validate_session_id(session_id: str) -> None:
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid session_id: {session_id!r}",
        ) from exc


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": None}
    )


@router.post("/login")
def login_submit(password: str = Form(...)) -> RedirectResponse:
    try:
        expected = require_operator_token()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    if not secrets.compare_digest(password, expected):
        return RedirectResponse(
            "/web/login?error=1", status_code=status.HTTP_303_SEE_OTHER
        )
    response = RedirectResponse("/web/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        OP_TOKEN_COOKIE,
        password,
        httponly=True,
        samesite="lax",
        secure=WEB_SECURE,
        max_age=60 * 60 * 24 * 7,
    )
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/web/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(OP_TOKEN_COOKIE)
    return response


# ---------------------------------------------------------------------------
# Session list / detail
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def sessions_list(
    request: Request, op_token: Optional[str] = Cookie(default=None)
):
    if not _is_authed(op_token):
        return _login_redirect()
    conn = _conn()
    try:
        sessions = db.list_sessions(conn, limit=50)
        return templates.TemplateResponse(
            request=request,
            name="sessions_list.html",
            context={"sessions": sessions},
        )
    finally:
        conn.close()


@router.get("/sessions/new", response_class=HTMLResponse)
def session_new_form(
    request: Request, op_token: Optional[str] = Cookie(default=None)
):
    if not _is_authed(op_token):
        return _login_redirect()
    return templates.TemplateResponse(
        request=request, name="session_new.html", context={"error": None}
    )


@router.post("/sessions/new")
def session_new_submit(
    problem_text: str = Form(...),
    nudge_window_secs: int = Form(60),
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    if not problem_text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="problem_text is required",
        )
    conn = _conn()
    try:
        session = workflow.start_session(
            conn,
            problem_text=problem_text,
            nudge_window_secs=nudge_window_secs,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    finally:
        conn.close()
    return RedirectResponse(
        f"/web/session/{session['id']}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/session/{session_id}", response_class=HTMLResponse)
def session_view(
    request: Request,
    session_id: str,
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_session_id(session_id)
    conn = _conn()
    try:
        session = db.get_session(conn, session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"session {session_id!r} not found",
            )
        rounds = db.list_rounds_for_session(conn, session_id)
        # Find the open awaiting_nudge transition (first by created order).
        cur = conn.execute(
            """SELECT i.* FROM iterations i
                 JOIN rounds r ON r.id = i.round_id
               WHERE r.session_id = ?
                 AND i.status = 'awaiting_nudge'
                 AND i.nudge_window_closes_at > ?
            ORDER BY i.source_emitted_at ASC LIMIT 1""",
            (session_id, datetime.now(timezone.utc).isoformat()),
        )
        row = cur.fetchone()
        pending = dict(row) if row else None
        return templates.TemplateResponse(
            request=request,
            name="session_view.html",
            context={
                "session": session,
                "rounds": rounds,
                "pending": pending,
            },
        )
    finally:
        conn.close()


@router.post("/session/{session_id}/nudge")
def session_nudge(
    session_id: str,
    iteration_id: str = Form(...),
    nudge_text: str = Form(""),
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_session_id(session_id)
    if not nudge_text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="nudge_text is required",
        )
    conn = _conn()
    try:
        db.apply_nudge(conn, iteration_id, nudge_text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    finally:
        conn.close()
    return RedirectResponse(
        f"/web/session/{session_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/session/{session_id}/skip")
def session_skip(
    session_id: str,
    iteration_id: str = Form(...),
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_session_id(session_id)
    conn = _conn()
    try:
        db.skip_nudge(conn, iteration_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    finally:
        conn.close()
    return RedirectResponse(
        f"/web/session/{session_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/session/{session_id}/abort")
def session_abort(
    session_id: str, op_token: Optional[str] = Cookie(default=None)
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_session_id(session_id)
    conn = _conn()
    try:
        rounds = db.list_rounds_for_session(conn, session_id)
        for rnd in rounds:
            if rnd["status"] in ("in_progress", "pending"):
                db.update_round_status(conn, rnd["id"], "aborted")
        db.update_session_status(conn, session_id, "aborted")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    finally:
        conn.close()
    return RedirectResponse("/web/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/session/{session_id}/transcript", response_class=HTMLResponse)
def session_transcript(
    request: Request,
    session_id: str,
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_session_id(session_id)
    conn = _conn()
    try:
        session = db.get_session(conn, session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"session {session_id!r} not found",
            )
        rounds = db.list_rounds_for_session(conn, session_id)
        iterations_by_round = {
            r["id"]: db.list_iterations_for_round(conn, r["id"]) for r in rounds
        }
        reviews_by_round = {
            r["id"]: db.list_reviews_for_round(conn, r["id"]) for r in rounds
        }
        return templates.TemplateResponse(
            request=request,
            name="session_transcript.html",
            context={
                "session": session,
                "rounds": rounds,
                "iterations_by_round": iterations_by_round,
                "reviews_by_round": reviews_by_round,
            },
        )
    finally:
        conn.close()


@router.get("/session/{session_id}/escalation", response_class=HTMLResponse)
def session_escalation(
    request: Request,
    session_id: str,
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_session_id(session_id)
    conn = _conn()
    try:
        session = db.get_session(conn, session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"session {session_id!r} not found",
            )
        rounds = db.list_rounds_for_session(conn, session_id)
        return templates.TemplateResponse(
            request=request,
            name="session_escalation.html",
            context={"session": session, "rounds": rounds},
        )
    finally:
        conn.close()


@router.post("/session/{session_id}/escalation/resolve")
def session_escalation_resolve(
    session_id: str,
    action: str = Form(...),
    iteration_id: str = Form(""),
    agent_id: str = Form(""),
    nudge_text: str = Form(""),
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_session_id(session_id)
    conn = _conn()
    try:
        workflow.resolve_escalation(
            conn,
            session_id,
            action=action,
            iteration_id=iteration_id or None,
            agent_id=agent_id or None,
            nudge_text=nudge_text or None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    finally:
        conn.close()
    return RedirectResponse(
        f"/web/session/{session_id}", status_code=status.HTTP_303_SEE_OTHER
    )
