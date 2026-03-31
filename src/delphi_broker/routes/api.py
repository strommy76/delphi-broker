"""
--------------------------------------------------------------------------------
FILE:        api.py
PATH:        C:/Projects/delphi-broker/src/delphi_broker/routes/api.py
DESCRIPTION: REST API endpoints. All authority-bearing mutations require
             HMAC-SHA256 signature verification.

CHANGELOG:
2026-03-31 17:30      Claude      [Harden] HMAC on all mutations, agent verify
                                     on ack, replay protection
2026-03-31 16:30      Claude      [Harden] HMAC on submit, agent verify on inbox
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import hmac as hmac_mod

from fastapi import APIRouter, HTTPException

from .. import database as db
from ..config import AGENT_SECRETS, DB_PATH
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


def _require_sig(agent_id: str, signature: str, timestamp: str, *fields: str) -> None:
    """Verify HMAC signature. Raises HTTPException on failure."""
    secret = AGENT_SECRETS.get(agent_id)
    if not secret:
        raise HTTPException(403, f"No secret configured for agent '{agent_id}'")
    if not signature or not timestamp:
        raise HTTPException(400, "Missing required fields: timestamp and signature")
    if not db.check_timestamp_freshness(timestamp):
        raise HTTPException(403, "Timestamp outside replay window (5 min)")
    expected = db.compute_signature(secret, *fields)
    if not hmac_mod.compare_digest(signature, expected):
        raise HTTPException(403, "Invalid signature — rejected")


@router.post("/messages")
def submit_message(payload: MessageSubmit):
    conn = _conn()
    try:
        if not db.verify_agent(conn, payload.sender):
            raise HTTPException(403, f"Unknown agent '{payload.sender}' — not in registry")
        _require_sig(
            payload.sender, payload.signature, payload.timestamp,
            "submit", payload.sender, payload.channel, payload.timestamp,
            payload.subject, payload.body, payload.recipients,
        )
        return db.submit_message(
            conn,
            sender=payload.sender,
            channel=payload.channel,
            subject=payload.subject,
            body=payload.body,
            recipients=payload.recipients,
            priority=payload.priority,
            parent_id=payload.parent_id,
            metadata=payload.metadata,
            signature=payload.signature,
            client_ts=payload.timestamp,
        )
    finally:
        conn.close()


@router.get("/messages/inbox")
def inbox(
    agent_id: str, channel: str = "", status: str = "APPROVED", since: str = "", limit: int = 50
):
    conn = _conn()
    try:
        if not db.verify_agent(conn, agent_id):
            raise HTTPException(403, f"Unknown agent '{agent_id}' — not in registry")
        db.touch_agent(conn, agent_id)
        exclude_acked = (status == "APPROVED")
        return db.list_messages(
            conn,
            status=status or None,
            channel=channel or None,
            recipient=agent_id,
            since=since or None,
            limit=limit,
            exclude_acked=exclude_acked,
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
        _require_sig(
            payload.agent_id, payload.signature, payload.timestamp,
            "approve", payload.agent_id, message_id, payload.timestamp,
        )
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
        _require_sig(
            payload.agent_id, payload.signature, payload.timestamp,
            "reject", payload.agent_id, message_id, payload.timestamp,
        )
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
        if not db.verify_agent(conn, payload.agent_id):
            raise HTTPException(403, f"Unknown agent '{payload.agent_id}' — not in registry")
        _require_sig(
            payload.agent_id, payload.signature, payload.timestamp,
            "ack", payload.agent_id, message_id, payload.timestamp,
        )
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
        _require_sig(
            payload.sender, payload.signature, payload.timestamp,
            "broadcast", payload.sender, payload.channel, payload.timestamp,
            payload.subject, payload.body,
        )
        status = "APPROVED" if payload.auto_approve else "PENDING"
        return db.submit_message(
            conn,
            sender=payload.sender,
            channel=payload.channel,
            subject=payload.subject,
            body=payload.body,
            recipients="*",
            priority=payload.priority,
            status=status,
            signature=payload.signature,
            client_ts=payload.timestamp,
        )
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


@router.get("/messages/{message_id}/receipts")
def get_receipts(message_id: str):
    conn = _conn()
    try:
        return db.get_receipts(conn, message_id)
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
