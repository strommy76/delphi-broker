"""Operator REST surface for the v2 broker.

All endpoints under `/api/v1/`. The operator authenticates with a single
session-creator token in the `X-Operator-Token` header, validated against
`config.OPERATOR_TOKEN`. Agents do not authenticate here — they go through
MCP (`mcp_server.py`).

This module performs no SQL directly: every persistence call goes through
`database.py`, every state transition through `workflow.py`. Failures map to
explicit `HTTPException`s so silent fallbacks are impossible.
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse, Response

from .. import database as db
from .. import workflow
from ..config import DB_PATH, require_operator_token
from ..models import (
    AbortResponse,
    ApproveExecutionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    EscalationAction,
    EscalationResolveRequest,
    EscalationResolveResponse,
    NudgeAction,
    NudgeRequest,
    NudgeResponse,
)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Auth + helpers
# ---------------------------------------------------------------------------


def verify_operator_token(
    x_operator_token: str | None = Header(default=None, alias="X-Operator-Token"),
) -> str:
    """Validate the operator token header against config.OPERATOR_TOKEN."""
    expected = require_operator_token()
    if not x_operator_token or not secrets.compare_digest(
        x_operator_token, expected
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid operator token",
        )
    return x_operator_token


def _conn() -> sqlite3.Connection:
    return db.get_connection(DB_PATH)


def _validate_session_id(session_id: str) -> None:
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid session_id: {session_id!r}",
        ) from exc


def _require_session(conn: sqlite3.Connection, session_id: str) -> dict:
    session = db.get_session(conn, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"session {session_id!r} not found",
        )
    return session


def _full_session_dict(conn: sqlite3.Connection, session_id: str) -> dict:
    """Return session + rounds + iterations + reviews as nested dict."""
    session = _require_session(conn, session_id)
    rounds = db.list_rounds_for_session(conn, session_id)
    current = db.current_round_for_session(conn, session_id)
    iterations_by_round: dict[str, list[dict]] = {}
    reviews_by_round: dict[str, list[dict]] = {}
    for rnd in rounds:
        iterations_by_round[rnd["id"]] = db.list_iterations_for_round(
            conn, rnd["id"]
        )
        reviews_by_round[rnd["id"]] = db.list_reviews_for_round(conn, rnd["id"])
    return {
        "session": session,
        "current_round": current,
        "rounds": rounds,
        "iterations_by_round": iterations_by_round,
        "reviews_by_round": reviews_by_round,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/session",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateSessionResponse,
    dependencies=[Depends(verify_operator_token)],
)
def create_session(payload: CreateSessionRequest) -> CreateSessionResponse:
    conn = _conn()
    try:
        session = workflow.start_session(
            conn,
            problem_text=payload.problem_text,
            nudge_window_secs=payload.nudge_window_secs,
        )
        return CreateSessionResponse(
            session_id=session["id"], status=session["status"]
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    finally:
        conn.close()


@router.get(
    "/session/{session_id}",
    dependencies=[Depends(verify_operator_token)],
)
def get_session(session_id: str) -> dict:
    _validate_session_id(session_id)
    conn = _conn()
    try:
        return _full_session_dict(conn, session_id)
    finally:
        conn.close()


@router.get(
    "/session/{session_id}/pending",
    dependencies=[Depends(verify_operator_token)],
)
def get_pending(session_id: str) -> Response:
    """Return the most recent iteration awaiting nudge whose window is open."""
    _validate_session_id(session_id)
    conn = _conn()
    try:
        _require_session(conn, session_id)
        # Find awaiting_nudge iterations on this session whose window is in
        # the future. Order by source_emitted_at ASC for determinism.
        cur = conn.execute(
            """SELECT i.* FROM iterations i
                 JOIN rounds r ON r.id = i.round_id
               WHERE r.session_id = ?
                 AND i.status = 'awaiting_nudge'
                 AND i.nudge_window_closes_at > ?
            ORDER BY i.source_emitted_at ASC""",
            (session_id, datetime.now(timezone.utc).isoformat()),
        )
        rows = [dict(row) for row in cur.fetchall()]
        if not rows:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return JSONResponse({"transition": rows[0]})
    finally:
        conn.close()


@router.post(
    "/session/{session_id}/nudge",
    response_model=NudgeResponse,
    dependencies=[Depends(verify_operator_token)],
)
def post_nudge(session_id: str, payload: NudgeRequest) -> NudgeResponse:
    _validate_session_id(session_id)
    conn = _conn()
    try:
        _require_session(conn, session_id)
        iteration = db.get_iteration(conn, payload.iteration_id)
        if iteration is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"iteration {payload.iteration_id!r} not found",
            )
        rnd = db.get_round(conn, iteration["round_id"])
        if rnd is None or rnd["session_id"] != session_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="iteration does not belong to this session",
            )
        if iteration["status"] != "awaiting_nudge":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"iteration is in status {iteration['status']!r}, "
                    "expected 'awaiting_nudge'"
                ),
            )
        if payload.action == NudgeAction.SUBMIT:
            if not payload.nudge_text or not payload.nudge_text.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="nudge_text is required when action='submit'",
                )
            updated = db.apply_nudge(conn, payload.iteration_id, payload.nudge_text)
        else:  # SKIP
            updated = db.skip_nudge(conn, payload.iteration_id)
        return NudgeResponse(
            iteration_id=updated["id"], status=updated["status"]
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    finally:
        conn.close()


@router.post(
    "/session/{session_id}/abort",
    response_model=AbortResponse,
    dependencies=[Depends(verify_operator_token)],
)
def post_abort(session_id: str) -> AbortResponse:
    """Abort the session. In-progress rounds are also marked aborted.

    Pending iterations are intentionally left in their current state for
    forensic review; no further agent dispatch happens after an abort
    because the session itself is terminal.
    """
    _validate_session_id(session_id)
    conn = _conn()
    try:
        _require_session(conn, session_id)
        rounds = db.list_rounds_for_session(conn, session_id)
        for rnd in rounds:
            if rnd["status"] in ("in_progress", "pending"):
                db.update_round_status(conn, rnd["id"], "aborted")
        updated = db.update_session_status(conn, session_id, "aborted")
        return AbortResponse(status=updated["status"])
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    finally:
        conn.close()


@router.post(
    "/session/{session_id}/escalation/resolve",
    response_model=EscalationResolveResponse,
    dependencies=[Depends(verify_operator_token)],
)
def post_resolve_escalation(
    session_id: str, payload: EscalationResolveRequest
) -> EscalationResolveResponse:
    _validate_session_id(session_id)
    conn = _conn()
    try:
        _require_session(conn, session_id)
        try:
            updated = workflow.resolve_escalation(
                conn,
                session_id,
                action=payload.action.value,
                iteration_id=payload.iteration_id,
                agent_id=payload.agent_id,
                nudge_text=payload.nudge_text,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        return EscalationResolveResponse(new_status=updated["status"])
    finally:
        conn.close()


@router.get(
    "/session/{session_id}/transcript",
    dependencies=[Depends(verify_operator_token)],
)
def get_transcript(session_id: str) -> dict:
    """Full ordered transcript: session + rounds + iterations + reviews."""
    _validate_session_id(session_id)
    conn = _conn()
    try:
        return _full_session_dict(conn, session_id)
    finally:
        conn.close()


@router.post(
    "/session/{session_id}/approve_execution",
    response_model=ApproveExecutionResponse,
    dependencies=[Depends(verify_operator_token)],
)
def post_approve_execution(session_id: str) -> ApproveExecutionResponse:
    """Reserved-for-future operator veto endpoint.

    The normal v2 workflow advances to `executing` automatically once round 3
    reaches consensus (see `workflow._spawn_execute_round`). This endpoint
    therefore acknowledges the current state without mutating it. Future
    work may add an explicit operator gate before the executor runs; until
    then this is a no-op for compatibility with the documented API surface.
    """
    _validate_session_id(session_id)
    conn = _conn()
    try:
        session = _require_session(conn, session_id)
        return ApproveExecutionResponse(status=session["status"])
    finally:
        conn.close()
