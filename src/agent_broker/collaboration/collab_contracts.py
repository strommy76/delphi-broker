"""
Contracts for operator-mediated collaboration.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..peer.peer_contracts import ParticipantRef

CollabErrorCode = Literal[
    "ack_idempotent",
    "auth_failed",
    "collaboration_required",
    "deliverable_not_found",
    "delivery_required",
    "draft_not_found",
    "forbidden_participant",
    "forbidden_recipient",
    "forbidden_thread",
    "idempotency_conflict",
    "invalid_payload",
    "thread_not_found",
    "unapproved_delivery",
    "unknown_participant",
]

DecisionType = Literal[
    "approve",
    "edit_and_approve",
    "redirect_and_approve",
    "reject",
    "operator_initiated",
]
OPERATOR_INITIATED_DECISION_TYPE: DecisionType = "operator_initiated"
OPERATOR_INITIATED_EVENT_KIND = "operator_initiated_message"


class StrictContract(BaseModel):
    """Base contract: reject unknown fields so boundary drift fails loud."""

    model_config = ConfigDict(extra="forbid")


class CollabError(StrictContract):
    error: CollabErrorCode
    reason: str
    detail: dict[str, Any] | None = Field(...)


def _require_text(value: str) -> str:
    if not value.strip():
        raise ValueError("text field must not be blank")
    return value.strip()


def collab_error(
    code: CollabErrorCode,
    reason: str,
    detail: dict[str, Any] | None,
) -> CollabError:
    return CollabError(error=code, reason=reason, detail=detail)


class CollabDraft(StrictContract):
    draft_id: str
    thread_id: str
    from_participant: ParticipantRef
    to_participants: tuple[ParticipantRef, ...]
    message_kind: str
    payload_json: dict[str, Any]
    content_text: str
    correlation_id: str
    created_ts: str


class CollabDecision(StrictContract):
    decision_id: str
    draft_id: str
    decision_type: DecisionType
    operator_participant: str
    final_payload_json: dict[str, Any] | None = Field(...)
    final_content_text: str | None = Field(...)
    reason: str | None = Field(...)
    decision_ts: str


class CollabDeliverable(StrictContract):
    deliverable_id: str
    draft_id: str
    decision_id: str
    thread_id: str
    from_participant: ParticipantRef
    to_participants: tuple[ParticipantRef, ...]
    message_kind: str
    payload_json: dict[str, Any]
    content_text: str
    correlation_id: str
    created_ts: str


class ProposeMessageRequest(StrictContract):
    from_participant: ParticipantRef
    to_participants: tuple[ParticipantRef, ...]
    message_kind: str
    payload_json: dict[str, Any]
    content_text: str
    correlation_id: str
    thread_id: str | None = Field(...)
    subject: str | None = Field(...)

    @field_validator("message_kind", "content_text", "correlation_id")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _require_text(value)


class ProposeMessageResponse(StrictContract):
    draft: CollabDraft | None = Field(...)
    error: CollabError | None = Field(...)


class OperatorDecisionRequest(StrictContract):
    operator_participant: ParticipantRef
    draft_id: str
    decision_type: DecisionType
    final_payload_json: dict[str, Any] | None = Field(...)
    final_content_text: str | None = Field(...)
    to_participants: tuple[ParticipantRef, ...] | None = Field(...)
    reason: str | None = Field(...)

    @field_validator("draft_id")
    @classmethod
    def _draft_id_not_blank(cls, value: str) -> str:
        return _require_text(value)


class OperatorDecisionResponse(StrictContract):
    decision: CollabDecision | None = Field(...)
    deliverable: CollabDeliverable | None = Field(...)
    error: CollabError | None = Field(...)


class OperatorMessageRequest(StrictContract):
    operator_participant: ParticipantRef
    to_participants: tuple[ParticipantRef, ...]
    message_kind: str
    payload_json: dict[str, Any]
    content_text: str
    correlation_id: str
    thread_id: str | None = Field(...)
    subject: str | None = Field(...)

    @field_validator("message_kind", "content_text", "correlation_id")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _require_text(value)


class OperatorMessageResponse(StrictContract):
    draft: CollabDraft | None = Field(...)
    decision: CollabDecision | None = Field(...)
    deliverable: CollabDeliverable | None = Field(...)
    error: CollabError | None = Field(...)


class CollabPollRequest(StrictContract):
    participant: ParticipantRef
    limit: int

    @field_validator("limit")
    @classmethod
    def _limit_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("limit must be >= 1")
        return value


class CollabPollResponse(StrictContract):
    deliverables: tuple[CollabDeliverable, ...]
    error: CollabError | None = Field(...)


class CollabAckRequest(StrictContract):
    participant: ParticipantRef
    deliverable_id: str

    @field_validator("deliverable_id")
    @classmethod
    def _deliverable_id_not_blank(cls, value: str) -> str:
        return _require_text(value)


class CollabAckResponse(StrictContract):
    deliverable_id: str
    acked_ts: str | None = Field(...)
    error: CollabError | None = Field(...)


class CollabGetThreadRequest(StrictContract):
    participant: ParticipantRef
    thread_id: str

    @field_validator("thread_id")
    @classmethod
    def _thread_id_not_blank(cls, value: str) -> str:
        return _require_text(value)


class CollabGetThreadResponse(StrictContract):
    thread_id: str
    entries: tuple[dict[str, Any], ...]
    error: CollabError | None = Field(...)
