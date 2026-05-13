"""
--------------------------------------------------------------------------------
FILE:        peer_contracts.py
PATH:        ~/projects/agent-broker/src/agent_broker/peer/peer_contracts.py
DESCRIPTION: Type-agnostic peer messaging contracts and canonical fail-loud error envelopes.

CHANGELOG:
2026-05-06 12:54      Codex      [Feature] Add is_probe participant contract field for operator-side transcript segregation.
2026-05-06 09:47      Codex      [Refactor] Add participant_type_mismatch peer error code for wrapper identity diagnostics.
2026-05-06 09:38      Codex      [Refactor] Tighten peer contract validators for required text and positive poll limits.
2026-05-06 09:35      Codex      [Feature] Add shared peer error helper for Phase 5 service responses.
2026-05-06 09:29      Codex      [Feature] Add Phase 4 peer messaging request, response, participant, and error contracts.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PeerErrorCode = Literal[
    "auth_failed",
    "collaboration_required",
    "unknown_participant",
    "forbidden_recipient",
    "invalid_payload",
    "replay_window_violation",
    "message_not_found",
    "ack_idempotent",
    "participant_type_mismatch",
]


class StrictContract(BaseModel):
    """Base contract: reject unknown fields so boundary drift fails loud."""

    model_config = ConfigDict(extra="forbid")


class ParticipantRef(StrictContract):
    participant_id: str
    participant_type: str
    transport_type: str
    is_probe: bool = False
    collaboration_governed: bool = False
    is_decision_authority: bool = Field(default=False, exclude=True)

    @field_validator("participant_id", "participant_type", "transport_type")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("participant fields must not be blank")
        return value.strip()


class PeerError(StrictContract):
    error: PeerErrorCode
    reason: str
    detail: dict[str, Any] | None = Field(...)


def _require_text(value: str) -> str:
    if not value.strip():
        raise ValueError("text field must not be blank")
    return value.strip()


def peer_error(
    code: PeerErrorCode,
    reason: str,
    detail: dict[str, Any] | None,
) -> PeerError:
    return PeerError(error=code, reason=reason, detail=detail)


class PeerMessage(StrictContract):
    message_id: str
    thread_id: str
    from_participant: ParticipantRef
    to_participants: tuple[ParticipantRef, ...] | None = Field(...)
    message_kind: str
    payload_json: dict[str, Any]
    content_text: str
    correlation_id: str
    parent_message_id: str | None = Field(...)
    sent_ts: str

    @field_validator(
        "message_id",
        "thread_id",
        "message_kind",
        "content_text",
        "correlation_id",
        "sent_ts",
    )
    @classmethod
    def _message_text_not_blank(cls, value: str) -> str:
        return _require_text(value)


class SendRequest(StrictContract):
    from_participant: ParticipantRef
    to_participants: tuple[ParticipantRef, ...] | None = Field(...)
    message_kind: str
    payload_json: dict[str, Any]
    content_text: str
    correlation_id: str
    parent_message_id: str | None = Field(...)
    thread_id: str | None = Field(...)
    subject: str | None = Field(...)

    @field_validator("message_kind", "content_text", "correlation_id")
    @classmethod
    def _send_text_not_blank(cls, value: str) -> str:
        return _require_text(value)


class SendResponse(StrictContract):
    message: PeerMessage | None = Field(...)
    error: PeerError | None = Field(...)


class PollRequest(StrictContract):
    participant: ParticipantRef
    limit: int

    @field_validator("limit")
    @classmethod
    def _limit_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("limit must be >= 1")
        return value


class PollResponse(StrictContract):
    messages: tuple[PeerMessage, ...]
    error: PeerError | None = Field(...)


class AckRequest(StrictContract):
    participant: ParticipantRef
    message_id: str

    @field_validator("message_id")
    @classmethod
    def _ack_message_id_not_blank(cls, value: str) -> str:
        return _require_text(value)


class AckResponse(StrictContract):
    message_id: str
    acked_ts: str | None = Field(...)
    error: PeerError | None = Field(...)


class GetThreadRequest(StrictContract):
    participant: ParticipantRef
    thread_id: str

    @field_validator("thread_id")
    @classmethod
    def _thread_id_not_blank(cls, value: str) -> str:
        return _require_text(value)


class GetThreadResponse(StrictContract):
    thread_id: str
    messages: tuple[PeerMessage, ...]
    error: PeerError | None = Field(...)
