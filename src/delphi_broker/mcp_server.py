"""
--------------------------------------------------------------------------------
FILE:        mcp_server.py
PATH:        C:/Projects/delphi-broker/src/delphi_broker/mcp_server.py
DESCRIPTION: MCP tool definitions for Delphi Broker. All authority-bearing
             mutations require HMAC-SHA256 signature verification.

CHANGELOG:
2026-03-31 17:30      Claude      [Harden] HMAC on approve/reject/ack/broadcast,
                                     agent verification on all paths, per-recipient
                                     ACK via receipts, replay protection
2026-03-31 16:30      Claude      [Harden] HMAC on submit, agent verification
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import hmac
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import database as db
from .config import AGENT_SECRETS, DB_PATH

mcp = FastMCP("delphi-broker")


def _conn():
    return db.get_connection(DB_PATH)


def _verify_sig(agent_id: str, signature: str, timestamp: str, *fields: str) -> Optional[str]:
    """Verify HMAC signature for an agent action. Returns error string or None."""
    secret = AGENT_SECRETS.get(agent_id)
    if not secret:
        return f"No secret configured for agent '{agent_id}'"
    if not signature or not timestamp:
        return "Missing required fields: timestamp and signature"
    if not db.check_timestamp_freshness(timestamp):
        return "Timestamp outside replay window (5 min)"
    expected = db.compute_signature(secret, *fields)
    if not hmac.compare_digest(signature, expected):
        return "Invalid signature — rejected"
    return None


@mcp.tool()
def delphi_submit(
    sender: str,
    channel: str,
    body: str,
    timestamp: str,
    signature: str,
    subject: str = "",
    recipients: str = "*",
    priority: str = "normal",
    parent_id: Optional[str] = None,
) -> dict:
    """Submit a message to a Delphi channel for approval.

    Messages enter PENDING status and require orchestrator approval before
    recipients can see them.

    Signature protocol:
      canonical = "submit|sender|channel|timestamp|subject|body|recipients|priority|parent_id|metadata_json"
      signature = HMAC-SHA256(secret, canonical)

    Args:
        sender: Your agent ID (e.g. 'dev-codex', 'prod-codex')
        channel: Target channel (e.g. '170-phase-1')
        body: Full message content. Markdown supported.
        timestamp: ISO 8601 timestamp of message creation
        signature: HMAC-SHA256 over canonical payload
        subject: Short summary line
        recipients: Comma-separated agent_ids, or '*' for all
        priority: 'normal' or 'urgent'
        parent_id: message_id of parent for threaded replies
    """
    conn = _conn()
    try:
        if not db.verify_agent(conn, sender):
            return {"error": f"Unknown agent '{sender}' — not in registry"}
        err = _verify_sig(
            sender,
            signature,
            timestamp,
            *db.build_submit_signature_fields(
                sender=sender,
                channel=channel,
                timestamp=timestamp,
                subject=subject,
                body=body,
                recipients=recipients,
                priority=priority,
                parent_id=parent_id,
                metadata=None,
            ),
        )
        if err:
            return {"error": err}
        return db.submit_message(
            conn,
            sender=sender,
            channel=channel,
            subject=subject,
            body=body,
            recipients=recipients,
            priority=priority,
            parent_id=parent_id,
            signature=signature,
            client_ts=timestamp,
        )
    finally:
        conn.close()


@mcp.tool()
def delphi_inbox(
    agent_id: str,
    channel: str = "",
    status: str = "APPROVED",
    since: str = "",
    limit: int = 20,
) -> dict:
    """Check your inbox for messages addressed to you or broadcast.

    Returns APPROVED messages you haven't yet acknowledged.

    Args:
        agent_id: Your agent ID
        channel: Filter to specific channel (empty = all)
        status: Filter by status: APPROVED, PENDING, REJECTED
        since: ISO 8601 timestamp — only messages after this time
        limit: Max messages to return
    """
    conn = _conn()
    try:
        if not db.verify_agent(conn, agent_id):
            return {"error": f"Unknown agent '{agent_id}' — not in registry"}
        db.touch_agent(conn, agent_id)
        exclude_acked = status == "APPROVED"
        messages = db.list_messages(
            conn,
            status=status or None,
            channel=channel or None,
            recipient=agent_id,
            since=since or None,
            limit=limit,
            exclude_acked=exclude_acked,
        )
        return {"agent_id": agent_id, "count": len(messages), "messages": messages}
    finally:
        conn.close()


@mcp.tool()
def delphi_pending(
    channel: str = "",
    limit: int = 20,
) -> dict:
    """View messages awaiting approval. Any agent can call this for visibility.

    Args:
        channel: Filter to specific channel (empty = all)
        limit: Max messages to return
    """
    conn = _conn()
    try:
        messages = db.list_messages(conn, status="PENDING", channel=channel or None, limit=limit)
        return {"count": len(messages), "messages": messages}
    finally:
        conn.close()


@mcp.tool()
def delphi_ack(
    message_id: str,
    agent_id: str,
    timestamp: str,
    signature: str,
) -> dict:
    """Acknowledge receipt of an approved message. Per-recipient tracking.

    Signature: HMAC-SHA256(secret, "ack|agent_id|message_id|timestamp")

    Args:
        message_id: The message UUID to acknowledge
        agent_id: Your agent ID
        timestamp: ISO 8601 timestamp
        signature: HMAC-SHA256 over canonical payload
    """
    conn = _conn()
    try:
        if not db.verify_agent(conn, agent_id):
            return {"error": f"Unknown agent '{agent_id}' — not in registry"}
        err = _verify_sig(
            agent_id,
            signature,
            timestamp,
            *db.build_ack_signature_fields(
                agent_id=agent_id,
                message_id=message_id,
                timestamp=timestamp,
            ),
        )
        if err:
            return {"error": err}
        message = db.get_message(conn, message_id)
        if not message or message["status"] != "APPROVED":
            return {"error": "Message not found or not in APPROVED status"}
        if not db.can_agent_ack_message(message, agent_id):
            return {"error": f"Agent '{agent_id}' is not a recipient of message '{message_id}'"}
        result = db.ack_message(conn, message_id, agent_id)
        if not result:
            return {"error": "Message not found or not in APPROVED status"}
        return result
    finally:
        conn.close()


@mcp.tool()
def delphi_approve(
    message_id: str,
    agent_id: str,
    timestamp: str,
    signature: str,
    note: str = "",
) -> dict:
    """Approve a pending message. Orchestrator-only.

    Signature: HMAC-SHA256(secret, "approve|agent_id|message_id|timestamp|note")

    Args:
        message_id: The message UUID to approve
        agent_id: Your agent ID (must have orchestrator role)
        timestamp: ISO 8601 timestamp
        signature: HMAC-SHA256 over canonical payload
        note: Optional approval note
    """
    conn = _conn()
    try:
        if not db.verify_agent(conn, agent_id):
            return {"error": f"Unknown agent '{agent_id}' — not in registry"}
        if not db.is_orchestrator(conn, agent_id):
            return {"error": f"Agent '{agent_id}' is not an orchestrator"}
        err = _verify_sig(
            agent_id,
            signature,
            timestamp,
            *db.build_approve_signature_fields(
                agent_id=agent_id,
                message_id=message_id,
                timestamp=timestamp,
                note=note,
            ),
        )
        if err:
            return {"error": err}
        result = db.approve_message(conn, message_id, agent_id, note)
        if not result:
            return {"error": "Message not found or not PENDING"}
        return result
    finally:
        conn.close()


@mcp.tool()
def delphi_reject(
    message_id: str,
    agent_id: str,
    timestamp: str,
    signature: str,
    reason: str = "",
) -> dict:
    """Reject a pending message. Orchestrator-only.

    Signature: HMAC-SHA256(secret, "reject|agent_id|message_id|timestamp|reason")

    Args:
        message_id: The message UUID to reject
        agent_id: Your agent ID (must have orchestrator role)
        timestamp: ISO 8601 timestamp
        signature: HMAC-SHA256 over canonical payload
        reason: Reason for rejection
    """
    conn = _conn()
    try:
        if not db.verify_agent(conn, agent_id):
            return {"error": f"Unknown agent '{agent_id}' — not in registry"}
        if not db.is_orchestrator(conn, agent_id):
            return {"error": f"Agent '{agent_id}' is not an orchestrator"}
        err = _verify_sig(
            agent_id,
            signature,
            timestamp,
            *db.build_reject_signature_fields(
                agent_id=agent_id,
                message_id=message_id,
                timestamp=timestamp,
                reason=reason,
            ),
        )
        if err:
            return {"error": err}
        result = db.reject_message(conn, message_id, agent_id, reason)
        if not result:
            return {"error": "Message not found or not PENDING"}
        return result
    finally:
        conn.close()


@mcp.tool()
def delphi_broadcast(
    sender: str,
    channel: str,
    body: str,
    timestamp: str,
    signature: str,
    subject: str = "",
    auto_approve: bool = True,
    priority: str = "normal",
) -> dict:
    """Broadcast a message to all agents. Orchestrator-only.

    Signature: HMAC-SHA256(secret, "broadcast|sender|channel|timestamp|subject|body|priority|auto_approve")

    Args:
        sender: Your agent ID (must have orchestrator role)
        channel: Target channel
        body: Full content (markdown)
        timestamp: ISO 8601 timestamp
        signature: HMAC-SHA256 over canonical payload
        subject: Short summary
        auto_approve: Skip approval gate (default True)
        priority: 'normal' or 'urgent'
    """
    conn = _conn()
    try:
        if not db.verify_agent(conn, sender):
            return {"error": f"Unknown agent '{sender}' — not in registry"}
        if not db.is_orchestrator(conn, sender):
            return {"error": f"Agent '{sender}' is not an orchestrator"}
        err = _verify_sig(
            sender,
            signature,
            timestamp,
            *db.build_broadcast_signature_fields(
                sender=sender,
                channel=channel,
                timestamp=timestamp,
                subject=subject,
                body=body,
                priority=priority,
                auto_approve=auto_approve,
            ),
        )
        if err:
            return {"error": err}
        status = "APPROVED" if auto_approve else "PENDING"
        return db.submit_message(
            conn,
            sender=sender,
            channel=channel,
            subject=subject,
            body=body,
            recipients="*",
            priority=priority,
            status=status,
            signature=signature,
            client_ts=timestamp,
        )
    finally:
        conn.close()
