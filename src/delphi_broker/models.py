"""
--------------------------------------------------------------------------------
FILE:        models.py
PATH:        C:/Projects/delphi-broker/src/delphi_broker/models.py
DESCRIPTION: Pydantic request/response models for all API surfaces.

CHANGELOG:
2026-03-31 17:30      Claude      [Harden] Add timestamp/signature to all
                                     authority-bearing mutation models
2026-03-31 16:30      Claude      [Harden] Add timestamp/signature to submit
--------------------------------------------------------------------------------
"""

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
    timestamp: str = ""
    signature: str = ""


class MessageDecision(BaseModel):
    agent_id: str
    note: str = ""
    timestamp: str = ""
    signature: str = ""


class MessageReject(BaseModel):
    agent_id: str
    reason: str = ""
    timestamp: str = ""
    signature: str = ""


class MessageAck(BaseModel):
    agent_id: str
    timestamp: str = ""
    signature: str = ""


class BroadcastSubmit(BaseModel):
    sender: str
    channel: str
    subject: str = ""
    body: str
    priority: str = "normal"
    auto_approve: bool = True
    timestamp: str = ""
    signature: str = ""


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
    parent_id: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    signature: Optional[str] = None


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
