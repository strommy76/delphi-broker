"""REST API endpoints for Delphi Broker."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import database as db
from ..config import DB_PATH
from ..models import (
    BroadcastSubmit,
    MessageAck,
    MessageDecision,
    MessageReject,
    MessageSubmit,
)

router = APIRouter(prefix="/api/v1")


def _conn():
    return db.get_connection(DB_PATH)


@router.post("/messages")
def submit_message(payload: MessageSubmit):
    conn = _conn()
    try:
        result = db.submit_message(
            conn,
            sender=payload.sender,
            channel=payload.channel,
            subject=payload.subject,
            body=payload.body,
            recipients=payload.recipients,
            priority=payload.priority,
            parent_id=payload.parent_id,
            metadata=payload.metadata,
        )
        return result
    finally:
        conn.close()


@router.get("/messages/inbox")
def inbox(
    agent_id: str, channel: str = "", status: str = "APPROVED", since: str = "", limit: int = 50
):
    conn = _conn()
    try:
        db.touch_agent(conn, agent_id)
        return db.list_messages(
            conn,
            status=status or None,
            channel=channel or None,
            recipient=agent_id,
            since=since or None,
            limit=limit,
        )
    finally:
        conn.close()


@router.get("/messages/pending")
def pending(channel: str = "", limit: int = 50):
    conn = _conn()
    try:
        return db.list_messages(
            conn,
            status="PENDING",
            channel=channel or None,
            limit=limit,
        )
    finally:
        conn.close()


@router.post("/messages/{message_id}/approve")
def approve(message_id: str, payload: MessageDecision):
    conn = _conn()
    try:
        if not db.is_orchestrator(conn, payload.agent_id):
            raise HTTPException(403, f"Agent '{payload.agent_id}' is not an orchestrator")
        result = db.approve_message(conn, message_id, payload.agent_id, payload.note)
        if not result:
            raise HTTPException(404, "Message not found or not PENDING")
        return result
    finally:
        conn.close()


@router.post("/messages/{message_id}/reject")
def reject(message_id: str, payload: MessageReject):
    conn = _conn()
    try:
        if not db.is_orchestrator(conn, payload.agent_id):
            raise HTTPException(403, f"Agent '{payload.agent_id}' is not an orchestrator")
        result = db.reject_message(conn, message_id, payload.agent_id, payload.reason)
        if not result:
            raise HTTPException(404, "Message not found or not PENDING")
        return result
    finally:
        conn.close()


@router.post("/messages/{message_id}/ack")
def ack(message_id: str, payload: MessageAck):
    conn = _conn()
    try:
        result = db.ack_message(conn, message_id, payload.agent_id)
        if not result:
            raise HTTPException(404, "Message not found or not APPROVED")
        return result
    finally:
        conn.close()


@router.post("/messages/broadcast")
def broadcast(payload: BroadcastSubmit):
    conn = _conn()
    try:
        if not db.is_orchestrator(conn, payload.sender):
            raise HTTPException(403, f"Agent '{payload.sender}' is not an orchestrator")
        status = "APPROVED" if payload.auto_approve else "PENDING"
        result = db.submit_message(
            conn,
            sender=payload.sender,
            channel=payload.channel,
            subject=payload.subject,
            body=payload.body,
            recipients="*",
            priority=payload.priority,
            status=status,
        )
        return result
    finally:
        conn.close()


@router.get("/messages/{message_id}")
def get_message(message_id: str):
    conn = _conn()
    try:
        msg = db.get_message(conn, message_id)
        if not msg:
            raise HTTPException(404, "Message not found")
        return msg
    finally:
        conn.close()


@router.get("/channels")
def channels():
    conn = _conn()
    try:
        return db.list_channels(conn)
    finally:
        conn.close()


@router.get("/agents")
def agents():
    conn = _conn()
    try:
        return db.list_agents(conn)
    finally:
        conn.close()
