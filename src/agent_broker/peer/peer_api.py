"""
--------------------------------------------------------------------------------
FILE:        peer_api.py
PATH:        ~/projects/agent-broker/src/agent_broker/peer/peer_api.py
DESCRIPTION: Operator JSON API for read-only peer transcript visibility and recipient read audit.

CHANGELOG:
2026-05-06 16:15      Codex      [Fix] Explicitly bypass transcript recipient
                                      check only inside operator-authorized routes.
2026-05-06 13:35      Codex      [Fix] Enforce explicit empty mark-read request bodies.
2026-05-06 13:01      Codex      [Fix] Bind mark-read authority to configured operator identity and plumb probe visibility.
2026-05-06 11:29      Codex      [Feature] Add Phase 7 peer transcript REST API with session/header operator auth.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Body, Cookie, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

from .. import database as db
from ..config import DB_PATH, OPERATOR_PARTICIPANT_ID, require_operator_token
from ..routes.web import OP_TOKEN_COOKIE
from .services import DELIVERY_SERVICE

router = APIRouter(prefix="/api/v1/peer")


class MarkReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


@router.get("/threads")
def list_threads(
    limit: int = 50,
    offset: int = 0,
    include_probes: bool = False,
    _operator=Depends(_verify_operator),
) -> dict:
    conn = _conn()
    try:
        return DELIVERY_SERVICE.list_threads(
            conn,
            limit=limit,
            offset=offset,
            include_probes=include_probes,
        )
    finally:
        conn.close()


@router.get("/threads/{thread_id}")
def get_thread(
    thread_id: str,
    include_probes: bool = False,
    _operator=Depends(_verify_operator),
) -> dict:
    conn = _conn()
    try:
        try:
            return DELIVERY_SERVICE.get_thread_transcript(
                conn,
                thread_id,
                include_probes=include_probes,
                # Operator auth owns transcript visibility here; agent
                # recipient authority remains enforced for non-operator calls.
                requires_recipient_check=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/messages/{message_id}")
def get_message(
    message_id: str,
    include_probes: bool = False,
    _operator=Depends(_verify_operator),
) -> dict:
    conn = _conn()
    try:
        try:
            return DELIVERY_SERVICE.get_message_detail(
                conn,
                message_id,
                include_probes=include_probes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/messages/{message_id}/mark_read")
def mark_read(
    message_id: str,
    _body: MarkReadBody = Body(default_factory=MarkReadBody),
    _operator=Depends(_verify_operator),
) -> dict:
    conn = _conn()
    try:
        try:
            return DELIVERY_SERVICE.mark_read(
                conn,
                message_id=message_id,
                recipient_participant=OPERATOR_PARTICIPANT_ID,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    finally:
        conn.close()
