"""v3 REST API — operator-facing task lifecycle.

Mounted under /api/v2 (note the version: v2 of the API, not v2 of the
workflow — the workflow is v3, which is unfortunate naming but reflects
the historical "API was v1, broker workflow was v2" path).

All endpoints gated by X-Operator-Token. Agents go through MCP, not REST.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from .. import database as db
from ..config import DB_PATH, require_operator_token
from . import database as v3db


router = APIRouter(prefix="/api/v2", tags=["v3"])


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _check_op_token(token: Optional[str]) -> None:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Operator-Token",
        )
    try:
        expected = require_operator_token()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc),
        ) from exc
    import secrets
    if not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid operator token",
        )


def _conn() -> sqlite3.Connection:
    return db.get_connection(DB_PATH)


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class CreateTaskRequest(BaseModel):
    title: str
    problem_text: str
    orchestrator_id: str
    task_json: Optional[dict[str, Any]] = None


class FinalizeTaskRequest(BaseModel):
    final_artifact: str
    final_artifact_json: Optional[dict[str, Any]] = None


class RefineTaskRequest(BaseModel):
    operator_comment: str


class AbortTaskRequest(BaseModel):
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Agent listing (for the orchestrator dropdown)
# ---------------------------------------------------------------------------


@router.get("/agents")
def list_agents(
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    """Return registered agents — the UI uses this to populate the
    orchestrator dropdown."""
    _check_op_token(x_operator_token)
    conn = _conn()
    try:
        cur = conn.execute(
            "SELECT agent_id, host, role FROM agents ORDER BY role, agent_id"
        )
        rows = cur.fetchall()
        return {
            "agents": [
                {"agent_id": r[0], "host": r[1], "role": r[2]} for r in rows
            ]
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
def create_task(
    body: CreateTaskRequest,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    _check_op_token(x_operator_token)
    conn = _conn()
    try:
        tid = v3db.create_task(
            conn,
            title=body.title,
            problem_text=body.problem_text,
            orchestrator_id=body.orchestrator_id,
            task_json=body.task_json,
        )
        return {"task_id": tid, "status": "new"}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    finally:
        conn.close()


@router.get("/tasks")
def list_tasks(
    status_filter: Optional[str] = None,
    orchestrator_id: Optional[str] = None,
    limit: int = 100,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    _check_op_token(x_operator_token)
    conn = _conn()
    try:
        return {
            "tasks": v3db.list_tasks(
                conn, status=status_filter, orchestrator_id=orchestrator_id, limit=limit,
            )
        }
    finally:
        conn.close()


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    _check_op_token(x_operator_token)
    conn = _conn()
    try:
        task = v3db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
        dispatches = v3db.list_dispatches(conn, task_id=task_id)
        outputs = v3db.get_outputs_for_task(conn, task_id)
        aggregations = v3db.list_aggregations(conn, task_id)
        events = v3db.list_events(conn, task_id)
        return {
            "task": task,
            "dispatches": dispatches,
            "outputs": outputs,
            "aggregations": aggregations,
            "events": events,
        }
    finally:
        conn.close()


@router.post("/tasks/{task_id}/approve")
def approve_task(
    task_id: str,
    body: FinalizeTaskRequest,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    _check_op_token(x_operator_token)
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
        v3db.finalize_task(
            conn, task_id,
            final_artifact=body.final_artifact,
            final_artifact_json=body.final_artifact_json,
        )
        return {"task_id": task_id, "status": "complete"}
    finally:
        conn.close()


@router.post("/tasks/{task_id}/refine")
def refine_task(
    task_id: str,
    body: RefineTaskRequest,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    """Operator sends a task back for more work with a comment."""
    _check_op_token(x_operator_token)
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
        # Log the operator's comment as an event the orchestrator can read,
        # then transition back to dispatched so the orchestrator picks it up.
        v3db.log_event(
            conn, task_id=task_id, event_type="operator_refine_comment",
            actor="operator", payload={"comment": body.operator_comment},
        )
        v3db.update_task_status(conn, task_id, "dispatched", actor="operator")
        return {"task_id": task_id, "status": "dispatched"}
    finally:
        conn.close()


@router.post("/tasks/{task_id}/abort")
def abort_task(
    task_id: str,
    body: AbortTaskRequest,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    _check_op_token(x_operator_token)
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
        if body.reason:
            v3db.log_event(
                conn, task_id=task_id, event_type="operator_abort_reason",
                actor="operator", payload={"reason": body.reason},
            )
        v3db.update_task_status(conn, task_id, "aborted", actor="operator")
        return {"task_id": task_id, "status": "aborted"}
    finally:
        conn.close()


@router.get("/tasks/{task_id}/events")
def list_events(
    task_id: str,
    limit: int = 500,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    _check_op_token(x_operator_token)
    conn = _conn()
    try:
        return {"events": v3db.list_events(conn, task_id, limit=limit)}
    finally:
        conn.close()
