"""MCP tool definitions for Delphi Broker."""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import database as db
from .config import DB_PATH

mcp = FastMCP("delphi-broker")


def _conn():
    return db.get_connection(DB_PATH)


@mcp.tool()
def delphi_submit(
    sender: str,
    channel: str,
    body: str,
    subject: str = "",
    recipients: str = "*",
    priority: str = "normal",
    parent_id: Optional[str] = None,
) -> dict:
    """Submit a message to a Delphi channel for approval.

    Messages enter PENDING status and require orchestrator approval before
    recipients can see them. Use this to send findings, prompt drafts,
    gate checkpoints, code diffs, or reviews to other agents.

    Args:
        sender: Your agent ID (e.g. 'dev-codex', 'prod-codex')
        channel: Target channel (e.g. '276-gate-h', '276-phase-1')
        body: Full message content. Markdown supported. Can be large.
        subject: Short summary line
        recipients: Comma-separated agent_ids, or '*' for all
        priority: 'normal' or 'urgent'
        parent_id: message_id of parent message for threaded replies
    """
    conn = _conn()
    try:
        return db.submit_message(
            conn,
            sender=sender,
            channel=channel,
            subject=subject,
            body=body,
            recipients=recipients,
            priority=priority,
            parent_id=parent_id,
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

    Call this to pick up approved messages from other agents.
    After processing a message, call delphi_ack to mark it handled.

    Args:
        agent_id: Your agent ID
        channel: Filter to specific channel (empty = all)
        status: Filter by status: APPROVED, PENDING, REJECTED, ACKED
        since: ISO 8601 timestamp — only messages after this time
        limit: Max messages to return
    """
    conn = _conn()
    try:
        db.touch_agent(conn, agent_id)
        messages = db.list_messages(
            conn,
            status=status or None,
            channel=channel or None,
            recipient=agent_id,
            since=since or None,
            limit=limit,
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
) -> dict:
    """Acknowledge receipt of an approved message. Transitions APPROVED -> ACKED.

    Call this after you have fully processed a message.

    Args:
        message_id: The message UUID to acknowledge
        agent_id: Your agent ID
    """
    conn = _conn()
    try:
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
    note: str = "",
) -> dict:
    """Approve a pending message, making it visible to recipients.
    Orchestrator-only.

    Args:
        message_id: The message UUID to approve
        agent_id: Your agent ID (must have orchestrator role)
        note: Optional approval note or additional context
    """
    conn = _conn()
    try:
        if not db.is_orchestrator(conn, agent_id):
            return {"error": f"Agent '{agent_id}' is not an orchestrator"}
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
    reason: str = "",
) -> dict:
    """Reject a pending message. Orchestrator-only.

    Args:
        message_id: The message UUID to reject
        agent_id: Your agent ID (must have orchestrator role)
        reason: Reason for rejection
    """
    conn = _conn()
    try:
        if not db.is_orchestrator(conn, agent_id):
            return {"error": f"Agent '{agent_id}' is not an orchestrator"}
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
    subject: str = "",
    auto_approve: bool = True,
    priority: str = "normal",
) -> dict:
    """Broadcast a message to all agents on a channel. Orchestrator-only.

    If auto_approve is True (default), the message skips the approval
    gate and is immediately visible. Use for directives, phase transitions,
    and coordination updates.

    Args:
        sender: Your agent ID (must have orchestrator role)
        channel: Target channel
        body: Full content (markdown)
        subject: Short summary
        auto_approve: Skip approval gate (default True)
        priority: 'normal' or 'urgent'
    """
    conn = _conn()
    try:
        if not db.is_orchestrator(conn, sender):
            return {"error": f"Agent '{sender}' is not an orchestrator"}
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
        )
    finally:
        conn.close()
