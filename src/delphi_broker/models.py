"""Pydantic models for Delphi Broker."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class MessageSubmit(BaseModel):
    sender: str
    channel: str
    subject: str = ""
    body: str
    recipients: str = "*"
    priority: str = "normal"
    parent_id: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class MessageDecision(BaseModel):
    agent_id: str
    note: str = ""


class MessageReject(BaseModel):
    agent_id: str
    reason: str = ""


class MessageAck(BaseModel):
    agent_id: str


class BroadcastSubmit(BaseModel):
    sender: str
    channel: str
    subject: str = ""
    body: str
    priority: str = "normal"
    auto_approve: bool = True


class MessageOut(BaseModel):
    message_id: str
    channel: str
    sender: str
    recipients: str
    subject: str
    body: str
    priority: str
    status: str
    submitted_at: str
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    decision_note: Optional[str] = None
    acked_at: Optional[str] = None
    acked_by: Optional[str] = None
    parent_id: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class AgentOut(BaseModel):
    agent_id: str
    host: str
    roles: str
    first_seen: str
    last_seen: str


class ChannelSummary(BaseModel):
    channel: str
    total: int
    pending: int
    approved: int
    rejected: int
    acked: int
