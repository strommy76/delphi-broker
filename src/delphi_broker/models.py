"""Pydantic models for the v2 iterative-pipeline data layer.

One model per persisted table, plus the enums that constrain status / role
columns. These are imported by the API layer for request and response
serialization; they intentionally mirror the shape returned by the DAO so the
boundary between persistence and HTTP stays thin.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SessionStatus(str, Enum):
    DRAFTING = "drafting"
    ROUND_1 = "round_1"
    ROUND_2 = "round_2"
    ROUND_3 = "round_3"
    EXECUTING = "executing"
    COMPLETE = "complete"
    ABORTED = "aborted"
    ESCALATED = "escalated"


class RoundType(str, Enum):
    SAME_HOST_PAIR = "same_host_pair"
    CROSS_HOST_ARBITRATION = "cross_host_arbitration"
    MULTI_AGENT_REVIEW = "multi_agent_review"
    EXECUTE = "execute"


class RoundStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    CONVERGED = "converged"
    ESCALATED = "escalated"
    COMPLETE = "complete"
    ABORTED = "aborted"


class IterationStatus(str, Enum):
    AWAITING_NUDGE = "awaiting_nudge"
    AWAITING_DESTINATION = "awaiting_destination"
    COMPLETE = "complete"
    OFF_SCRIPT = "off_script"


class SelfAssessment(str, Enum):
    CONVERGED = "converged"
    MORE_WORK_NEEDED = "more_work_needed"


class ReviewDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class AgentRole(str, Enum):
    WORKER = "worker"
    ARBITRATOR = "arbitrator"
    EXECUTOR = "executor"


# ---------------------------------------------------------------------------
# Persisted rows
# ---------------------------------------------------------------------------


class Session(BaseModel):
    id: str
    problem_text: str
    status: SessionStatus
    nudge_window_secs: int = Field(default=60, ge=0)
    created_at: str
    updated_at: str
    finalized_prompt: Optional[str] = None


class Round(BaseModel):
    id: str
    session_id: str
    round_num: int = Field(ge=1)
    round_type: RoundType
    host: Optional[str] = None
    status: RoundStatus
    started_at: str
    ended_at: Optional[str] = None
    outcome_text: Optional[str] = None


class Iteration(BaseModel):
    id: str
    round_id: str
    iter_num: int = Field(ge=1)
    source_agent: Optional[str] = None
    destination_agent: str
    source_output: str
    nudge_text: Optional[str] = None
    nudge_window_closes_at: str
    destination_output: Optional[str] = None
    destination_self_assess: Optional[SelfAssessment] = None
    destination_rationale: Optional[str] = None
    source_emitted_at: str
    destination_received_at: Optional[str] = None
    destination_emitted_at: Optional[str] = None
    status: IterationStatus


class Review(BaseModel):
    id: str
    round_id: str
    reviewer_agent: str
    decision: ReviewDecision
    comments: Optional[str] = None
    rationale: Optional[str] = None
    emitted_at: str


class Agent(BaseModel):
    agent_id: str
    host: str
    role: AgentRole
    first_seen: str
    last_seen: str


# ---------------------------------------------------------------------------
# Request / response models (REST API)
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    problem_text: str = Field(min_length=1)
    nudge_window_secs: int = Field(default=60, ge=0)


class CreateSessionResponse(BaseModel):
    session_id: str
    status: SessionStatus


class NudgeAction(str, Enum):
    SUBMIT = "submit"
    SKIP = "skip"


class NudgeRequest(BaseModel):
    iteration_id: str = Field(min_length=1)
    action: NudgeAction
    nudge_text: Optional[str] = None


class NudgeResponse(BaseModel):
    ok: bool = True
    iteration_id: str
    status: IterationStatus


class AbortResponse(BaseModel):
    ok: bool = True
    status: SessionStatus


class EscalationAction(str, Enum):
    FORCE_CONVERGE = "force_converge"
    RETRY = "retry"
    ABORT = "abort"
    SKIP_AGENT = "skip_agent"
    PROCEED_TO_ARBITRATOR = "proceed_to_arbitrator"


class EscalationResolveRequest(BaseModel):
    action: EscalationAction
    iteration_id: Optional[str] = None
    agent_id: Optional[str] = None
    nudge_text: Optional[str] = None


class EscalationResolveResponse(BaseModel):
    ok: bool = True
    new_status: SessionStatus


class ApproveExecutionResponse(BaseModel):
    ok: bool = True
    status: SessionStatus


__all__ = [
    "AgentRole",
    "IterationStatus",
    "ReviewDecision",
    "RoundStatus",
    "RoundType",
    "SelfAssessment",
    "SessionStatus",
    "Session",
    "Round",
    "Iteration",
    "Review",
    "Agent",
    "CreateSessionRequest",
    "CreateSessionResponse",
    "NudgeAction",
    "NudgeRequest",
    "NudgeResponse",
    "AbortResponse",
    "EscalationAction",
    "EscalationResolveRequest",
    "EscalationResolveResponse",
    "ApproveExecutionResponse",
]
