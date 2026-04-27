"""v3 web UI — phone-friendly task lifecycle for the operator.

Mounted under /web/v3. Reuses cookie auth + base.html / style.css from
v2's web router. Three pages:

  /web/v3/                — task list (active first, then recent)
  /web/v3/new             — task creation form (with orchestrator dropdown)
  /web/v3/{id}            — task view (live state + approve/refine/abort)

Form actions on the task view post to /web/v3/{id}/{action}, which then
redirects back to the same page with a status flash.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import database as db
from ..config import DB_PATH, require_operator_token
from . import database as v3db


router = APIRouter(prefix="/web/v3", tags=["v3-web"])

_templates_dir = str(Path(__file__).resolve().parent.parent / "templates")
templates = Jinja2Templates(directory=_templates_dir)

OP_TOKEN_COOKIE = "op_token"


# ---------------------------------------------------------------------------
# Auth helpers (mirrors v2 routes/web.py)
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


def _validate_task_id(task_id: str) -> None:
    try:
        uuid.UUID(task_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid task_id: {task_id!r}",
        ) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def tasks_list(
    request: Request, op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    conn = _conn()
    try:
        # Active first, then recent terminal ones
        active = v3db.list_tasks(conn, limit=200)
    finally:
        conn.close()
    # Sort: non-terminal first, by updated_at desc; then terminal by completed_at desc
    terminal = {"complete", "aborted"}
    active.sort(key=lambda t: (t["status"] in terminal, -ord(t["updated_at"][0])))
    return templates.TemplateResponse(
        request=request,
        name="v3_tasks_list.html",
        context={"tasks": active},
    )


@router.get("/new", response_class=HTMLResponse)
def new_task_form(
    request: Request, op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    conn = _conn()
    try:
        agents = [
            {"agent_id": r[0], "host": r[1], "role": r[2]}
            for r in conn.execute(
                "SELECT agent_id, host, role FROM agents ORDER BY role, agent_id"
            )
        ]
    finally:
        conn.close()
    # Default orchestrator: pi-claude per project memory (independence from Lexx);
    # operator can override.
    default_orch = "pi-claude" if any(a["agent_id"] == "pi-claude" for a in agents) else (agents[0]["agent_id"] if agents else "")
    return templates.TemplateResponse(
        request=request,
        name="v3_task_new.html",
        context={"agents": agents, "default_orchestrator": default_orch, "error": None},
    )


@router.post("/new")
def new_task_submit(
    title: str = Form(...),
    orchestrator_id: str = Form(...),
    problem_text: str = Form(...),
    task_json: str = Form(default=""),
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    parsed_json: Optional[dict] = None
    if task_json.strip():
        try:
            parsed_json = json.loads(task_json)
        except json.JSONDecodeError as exc:
            # Re-render with error
            from fastapi.responses import HTMLResponse as _H  # local import to avoid shadowing
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"task_json is not valid JSON: {exc}",
            )
    conn = _conn()
    try:
        task_id = v3db.create_task(
            conn, title=title.strip(), problem_text=problem_text,
            orchestrator_id=orchestrator_id, task_json=parsed_json,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    finally:
        conn.close()
    return RedirectResponse(f"/web/v3/{task_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{task_id}", response_class=HTMLResponse)
def task_view(
    task_id: str,
    request: Request,
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_task_id(task_id)
    conn = _conn()
    try:
        task = v3db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
        dispatches = v3db.list_dispatches(conn, task_id=task_id)
        outputs = v3db.get_outputs_for_task(conn, task_id)
        aggregations = v3db.list_aggregations(conn, task_id)
        events = v3db.list_events(conn, task_id)
    finally:
        conn.close()
    # Index outputs by dispatch_id for the template
    outputs_by_dispatch = {o["dispatch_id"]: o for o in outputs}
    return templates.TemplateResponse(
        request=request,
        name="v3_task_view.html",
        context={
            "task": task,
            "dispatches": dispatches,
            "outputs_by_dispatch": outputs_by_dispatch,
            "aggregations": aggregations,
            "events": events,
        },
    )


@router.post("/{task_id}/approve")
def task_approve(
    task_id: str,
    final_artifact: str = Form(...),
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_task_id(task_id)
    conn = _conn()
    try:
        task = v3db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
        if task["status"] != "awaiting_approval":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"task is in status {task['status']!r}, expected 'awaiting_approval'",
            )
        v3db.finalize_task(conn, task_id, final_artifact=final_artifact)
    finally:
        conn.close()
    return RedirectResponse(f"/web/v3/{task_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{task_id}/refine")
def task_refine(
    task_id: str,
    operator_comment: str = Form(...),
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_task_id(task_id)
    conn = _conn()
    try:
        task = v3db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
        if task["status"] != "awaiting_approval":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"refine only valid from 'awaiting_approval'; current is {task['status']!r}",
            )
        v3db.log_event(
            conn, task_id=task_id, event_type="operator_refine_comment",
            actor="operator", payload={"comment": operator_comment},
        )
        v3db.update_task_status(conn, task_id, "dispatched", actor="operator")
    finally:
        conn.close()
    return RedirectResponse(f"/web/v3/{task_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{task_id}/abort")
def task_abort(
    task_id: str,
    reason: str = Form(default=""),
    op_token: Optional[str] = Cookie(default=None),
):
    if not _is_authed(op_token):
        return _login_redirect()
    _validate_task_id(task_id)
    conn = _conn()
    try:
        task = v3db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
        if task["status"] in ("complete", "aborted"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"task already in terminal status {task['status']!r}",
            )
        if reason.strip():
            v3db.log_event(
                conn, task_id=task_id, event_type="operator_abort_reason",
                actor="operator", payload={"reason": reason.strip()},
            )
        v3db.update_task_status(conn, task_id, "aborted", actor="operator")
    finally:
        conn.close()
    return RedirectResponse(f"/web/v3/{task_id}", status_code=status.HTTP_303_SEE_OTHER)
