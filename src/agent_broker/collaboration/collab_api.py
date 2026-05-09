"""Operator JSON API for collaboration drafts and transcripts."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Body, Cookie, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

from .. import database as db
from ..config import DB_PATH, OPERATOR_PARTICIPANT_ID, require_operator_token
from ..peer.peer_contracts import ParticipantRef
from ..routes.web import OP_TOKEN_COOKIE
from .collab_contracts import CollabGetThreadRequest, OperatorDecisionRequest
from .services import COLLABORATION_SERVICE, IDENTITY_SERVICE

router = APIRouter(prefix="/api/v1/collab")


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EditApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_text: str
    payload_json: dict[str, Any] | None = None
    reason: str | None = None


class RedirectApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_participants: list[str]
    reason: str | None = None


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


def _conn():
    return db.get_connection(DB_PATH)


def _verify_operator(
    x_operator_token: str | None = Header(default=None, alias="X-Operator-Token"),
    op_token: str | None = Cookie(default=None, alias=OP_TOKEN_COOKIE),
) -> None:
    expected = require_operator_token()
    supplied = x_operator_token or op_token
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid operator token",
        )


def _operator_ref() -> ParticipantRef:
    operator = IDENTITY_SERVICE.resolve(OPERATOR_PARTICIPANT_ID)
    if operator is None:
        raise RuntimeError(f"operator participant {OPERATOR_PARTICIPANT_ID!r} is not registered")
    return operator


def _participants(ids: list[str]) -> tuple[ParticipantRef, ...]:
    participants = []
    for participant_id in ids:
        participant = IDENTITY_SERVICE.resolve(participant_id)
        if participant is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown participant {participant_id!r}",
            )
        participants.append(participant)
    return tuple(participants)


@router.get("/drafts")
def list_pending_drafts(
    include_probes: bool = False,
    _operator=Depends(_verify_operator),
) -> dict:
    conn = _conn()
    try:
        return COLLABORATION_SERVICE.list_pending_drafts(conn, include_probes=include_probes)
    finally:
        conn.close()


@router.post("/drafts/{draft_id}/approve")
def approve_draft(
    draft_id: str,
    _body: EmptyBody | None = Body(default=None),
    _operator=Depends(_verify_operator),
) -> dict:
    return _decide(
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


@router.post("/drafts/{draft_id}/edit_approve")
def edit_approve_draft(
    draft_id: str,
    body: EditApproveBody,
    _operator=Depends(_verify_operator),
) -> dict:
    return _decide(
        OperatorDecisionRequest(
            operator_participant=_operator_ref(),
            draft_id=draft_id,
            decision_type="edit_and_approve",
            final_payload_json=body.payload_json,
            final_content_text=body.content_text,
            to_participants=None,
            reason=body.reason,
        )
    )


@router.post("/drafts/{draft_id}/redirect_approve")
def redirect_approve_draft(
    draft_id: str,
    body: RedirectApproveBody,
    _operator=Depends(_verify_operator),
) -> dict:
    return _decide(
        OperatorDecisionRequest(
            operator_participant=_operator_ref(),
            draft_id=draft_id,
            decision_type="redirect_and_approve",
            final_payload_json=None,
            final_content_text=None,
            to_participants=_participants(body.to_participants),
            reason=body.reason,
        )
    )


@router.post("/drafts/{draft_id}/reject")
def reject_draft(
    draft_id: str,
    body: RejectBody | None = Body(default=None),
    _operator=Depends(_verify_operator),
) -> dict:
    return _decide(
        OperatorDecisionRequest(
            operator_participant=_operator_ref(),
            draft_id=draft_id,
            decision_type="reject",
            final_payload_json=None,
            final_content_text=None,
            to_participants=None,
            reason=None if body is None else body.reason,
        )
    )


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str, _operator=Depends(_verify_operator)) -> dict:
    conn = _conn()
    try:
        response = COLLABORATION_SERVICE.get_thread(
            conn,
            request=CollabGetThreadRequest(
                participant=_operator_ref(),
                thread_id=thread_id,
            ),
        )
        if response.error is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=response.error.reason)
        return response.model_dump(mode="json")
    finally:
        conn.close()


def _decide(request: OperatorDecisionRequest) -> dict:
    conn = _conn()
    try:
        response = COLLABORATION_SERVICE.decide(conn, request)
        if response.error is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=response.error.reason
            )
        return response.model_dump(mode="json")
    finally:
        conn.close()
