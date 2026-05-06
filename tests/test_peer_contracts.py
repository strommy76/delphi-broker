"""
--------------------------------------------------------------------------------
FILE:        test_peer_contracts.py
PATH:        ~/projects/agent-broker/tests/test_peer_contracts.py
DESCRIPTION: Contract tests for type-agnostic peer messaging request, response, participant, and error models.

CHANGELOG:
2026-05-06 13:14      Codex      [Feature] Add participant probe-flag contract coverage.
2026-05-06 09:29      Codex      [Feature] Add Phase 4 peer messaging contract coverage.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_broker.peer.peer_contracts import (
    AckRequest,
    PeerError,
    ParticipantRef,
    SendRequest,
)


def test_send_request_requires_explicit_optional_boundaries():
    sender = ParticipantRef(
        participant_id="pi-claude",
        participant_type="agent",
        transport_type="mcp",
    )
    request = SendRequest(
        from_participant=sender,
        to_participants=None,
        message_kind="text",
        payload_json={"body": "hello"},
        content_text="hello",
        correlation_id="corr-1",
        parent_message_id=None,
        thread_id=None,
        subject="coordination",
    )
    assert request.to_participants is None
    assert request.parent_message_id is None
    assert request.thread_id is None


def test_contracts_reject_unknown_fields():
    with pytest.raises(ValidationError):
        AckRequest(
            participant=ParticipantRef(
                participant_id="pi-codex",
                participant_type="agent",
                transport_type="mcp",
            ),
            message_id="msg-1",
            ambient_default=True,
        )


def test_participant_ref_rejects_blank_identity_fields():
    with pytest.raises(ValidationError):
        ParticipantRef(
            participant_id=" ",
            participant_type="agent",
            transport_type="mcp",
        )


def test_participant_ref_probe_flag_defaults_false_and_accepts_true():
    default = ParticipantRef(
        participant_id="pi-codex",
        participant_type="agent",
        transport_type="mcp",
    )
    probe = ParticipantRef(
        participant_id="pi-codex-probe",
        participant_type="agent",
        transport_type="http",
        is_probe=True,
    )
    assert default.is_probe is False
    assert probe.is_probe is True


def test_peer_error_codes_are_canonical():
    err = PeerError(
        error="forbidden_recipient",
        reason="self messaging is not allowed",
        detail={"participant": "pi-codex"},
    )
    assert err.error == "forbidden_recipient"

    with pytest.raises(ValidationError):
        PeerError(error="silent_fallback", reason="no", detail=None)
