"""Operator web UI for collaboration drafts and transcripts."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Cookie, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import database as db
from ..config import DB_PATH, OPERATOR_PARTICIPANT_ID, require_operator_token
from ..peer.peer_contracts import ParticipantRef
from ..routes.web import OP_TOKEN_COOKIE, templates
from .collab_contracts import CollabGetThreadRequest, OperatorDecisionRequest
from .services import COLLABORATION_SERVICE, IDENTITY_SERVICE

router = APIRouter(prefix="/web/collab")


def _conn():
    return db.get_connection(DB_PATH)


def _require_web_operator(op_token: str | None) -> None:
    expected = require_operator_token()
    if not op_token or not secrets.compare_digest(op_token, expected):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/web/login"}
        )


def _operator_ref() -> ParticipantRef:
    operator = IDENTITY_SERVICE.resolve(OPERATOR_PARTICIPANT_ID)
    if operator is None:
        raise RuntimeError(f"operator participant {OPERATOR_PARTICIPANT_ID!r} is not registered")
    return operator


@router.get("/drafts", response_class=HTMLResponse)
def drafts_list(
    request: Request,
    include_probes: bool = Query(default=False),
    op_token: str | None = Cookie(default=None, alias=OP_TOKEN_COOKIE),
):
    _require_web_operator(op_token)
    conn = _conn()
    try:
        payload = COLLABORATION_SERVICE.list_pending_drafts(
            conn,
            include_probes=include_probes,
        )
    finally:
        conn.close()
    return templates.TemplateResponse(
        name="collab_drafts.html",
        context={
            "request": request,
            "payload": payload,
            "include_probes": include_probes,
        },
    )


@router.post("/drafts/{draft_id}/approve")
def approve_draft(
    draft_id: str,
    op_token: str | None = Cookie(default=None, alias=OP_TOKEN_COOKIE),
):
    _require_web_operator(op_token)
    _decide(
        OperatorDecisionRequest(
            operator_participant=_operator_ref(),
            draft_id=draft_id,
            decision_type="approve",
            final_payload_json=None,
            final_content_text=None,
            to_participants=None,
            reason=None,
        )
    )
    return RedirectResponse("/web/collab/drafts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/drafts/{draft_id}/edit_approve")
def edit_approve_draft(
    draft_id: str,
    content_text: str = Form(...),
    op_token: str | None = Cookie(default=None, alias=OP_TOKEN_COOKIE),
):
    _require_web_operator(op_token)
    _decide(
        OperatorDecisionRequest(
            operator_participant=_operator_ref(),
            draft_id=draft_id,
            decision_type="edit_and_approve",
            final_payload_json=None,
            final_content_text=content_text,
            to_participants=None,
            reason=None,
        )
    )
    return RedirectResponse("/web/collab/drafts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/drafts/{draft_id}/reject")
def reject_draft(
    draft_id: str,
    reason: str = Form(default=""),
    op_token: str | None = Cookie(default=None, alias=OP_TOKEN_COOKIE),
):
    _require_web_operator(op_token)
    _decide(
        OperatorDecisionRequest(
            operator_participant=_operator_ref(),
            draft_id=draft_id,
            decision_type="reject",
            final_payload_json=None,
            final_content_text=None,
            to_participants=None,
            reason=reason or None,
        )
    )
    return RedirectResponse("/web/collab/drafts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/drafts/{draft_id}/redirect_approve")
def redirect_approve_draft(
    draft_id: str,
    to_participants: str = Form(...),
    op_token: str | None = Cookie(default=None, alias=OP_TOKEN_COOKIE),
):
    _require_web_operator(op_token)
    recipients = []
    for participant_id in [item.strip() for item in to_participants.split(",") if item.strip()]:
        participant = IDENTITY_SERVICE.resolve(participant_id)
        if participant is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown participant {participant_id!r}",
            )
        recipients.append(participant)
    _decide(
        OperatorDecisionRequest(
            operator_participant=_operator_ref(),
            draft_id=draft_id,
            decision_type="redirect_and_approve",
            final_payload_json=None,
            final_content_text=None,
            to_participants=tuple(recipients),
            reason=None,
        )
    )
    return RedirectResponse("/web/collab/drafts", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/threads/{thread_id}", response_class=HTMLResponse)
def thread_view(
    request: Request,
    thread_id: str,
    op_token: str | None = Cookie(default=None, alias=OP_TOKEN_COOKIE),
):
    _require_web_operator(op_token)
    conn = _conn()
    try:
        payload = COLLABORATION_SERVICE.get_thread(
            conn,
            CollabGetThreadRequest(participant=_operator_ref(), thread_id=thread_id),
        )
    finally:
        conn.close()
    if payload.error is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=payload.error.reason)
    return templates.TemplateResponse(
        name="collab_thread.html",
        context={"request": request, "payload": payload.model_dump(mode="json")},
    )


def _decide(request: OperatorDecisionRequest) -> None:
    conn = _conn()
    try:
        response = COLLABORATION_SERVICE.decide(conn, request)
        if response.error is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=response.error.reason
            )
    finally:
        conn.close()
