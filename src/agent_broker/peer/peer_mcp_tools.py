"""
--------------------------------------------------------------------------------
FILE:        peer_mcp_tools.py
PATH:        ~/projects/agent-broker/src/agent_broker/peer/peer_mcp_tools.py
DESCRIPTION: HMAC-authenticated MCP adapters for peer messaging send, poll, ack, and thread retrieval.

CHANGELOG:
2026-05-06 13:00      Codex      [Refactor] Use shared peer service singletons across MCP, API, and web boundaries.
2026-05-06 11:16      Codex      [Fix] Remove per-request schema init, reuse peer services, and expose explicit participant identity claims.
2026-05-06 09:55      Codex      [Feature] Add Phase 6 peer MCP tool registration over the peer delivery service.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .peer_contracts import (
    AckRequest,
    AckResponse,
    GetThreadRequest,
    GetThreadResponse,
    ParticipantRef,
    PollRequest,
    PollResponse,
    SendRequest,
    SendResponse,
    peer_error,
)
from .peer_delivery_service import PeerDeliveryService
from .services import DELIVERY_SERVICE, IDENTITY_SERVICE


def _invalid_payload_response(response_type, **fields) -> dict:
    return response_type(
        **fields,
        error=peer_error("invalid_payload", "peer MCP payload validation failed", None),
    ).model_dump(mode="json")


def register_peer_tools(mcp, _verify, _conn, AGENT_SECRETS) -> None:
    """Register peer messaging MCP tools.

    HMAC auth is adapted here; participant identity is explicit and then
    passed to peer services as an already-authenticated boundary object.
    """

    def _service() -> PeerDeliveryService:
        return DELIVERY_SERVICE

    def _identity():
        return IDENTITY_SERVICE

    def _participant(agent_id: str, participant_type: str, transport_type: str) -> ParticipantRef:
        resolved = _identity().resolve(agent_id)
        return ParticipantRef(
            participant_id=agent_id,
            participant_type=participant_type,
            transport_type=transport_type,
            is_probe=bool(resolved and resolved.is_probe),
            collaboration_governed=bool(resolved and resolved.collaboration_governed),
            is_decision_authority=bool(resolved and resolved.is_decision_authority),
        )

    @mcp.tool(
        name="peer_send",
        description=(
            "Send a type-agnostic peer message. Signature canonical: "
            "'peer_send|<agent_id>|<participant_type>|<transport_type>|<client_ts>|<correlation_id>'."
        ),
    )
    def peer_send(
        agent_id: str,
        participant_type: str,
        transport_type: str,
        client_ts: str,
        signature: str,
        to_participants: list[str] | None,
        message_kind: str,
        payload_json: dict[str, Any],
        content_text: str,
        correlation_id: str,
        parent_message_id: str | None,
        thread_id: str | None,
        subject: str | None,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn,
                agent_id,
                client_ts,
                signature,
                (
                    "peer_send",
                    agent_id,
                    participant_type,
                    transport_type,
                    client_ts,
                    correlation_id,
                ),
            )
            if err:
                return err
            try:
                sender = _participant(agent_id, participant_type, transport_type)
                recipients = None
                if to_participants is not None:
                    identity = _identity()
                    resolved_recipients = []
                    for item in to_participants:
                        recipient = identity.resolve(item)
                        if recipient is None:
                            return SendResponse(
                                message=None,
                                error=peer_error(
                                    "unknown_participant",
                                    f"unknown recipient {item!r}",
                                    {"participant_id": item},
                                ),
                            ).model_dump(mode="json")
                        resolved_recipients.append(recipient)
                    recipients = tuple(resolved_recipients)
                request = SendRequest(
                    from_participant=sender,
                    to_participants=recipients,
                    message_kind=message_kind,
                    payload_json=payload_json,
                    content_text=content_text,
                    correlation_id=correlation_id,
                    parent_message_id=parent_message_id,
                    thread_id=thread_id,
                    subject=subject,
                )
            except ValidationError:
                return _invalid_payload_response(SendResponse, message=None)
            response = _service().send(conn, request)
            return response.model_dump(mode="json")
        finally:
            conn.close()

    @mcp.tool(
        name="peer_poll",
        description=(
            "Poll unacked peer messages addressed to the caller. Signature "
            "canonical: 'peer_poll|<agent_id>|<participant_type>|<transport_type>|<client_ts>|<limit>'."
        ),
    )
    def peer_poll(
        agent_id: str,
        participant_type: str,
        transport_type: str,
        client_ts: str,
        signature: str,
        limit: int,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn,
                agent_id,
                client_ts,
                signature,
                ("peer_poll", agent_id, participant_type, transport_type, client_ts, str(limit)),
            )
            if err:
                return err
            try:
                request = PollRequest(
                    participant=_participant(agent_id, participant_type, transport_type),
                    limit=limit,
                )
            except ValidationError:
                return _invalid_payload_response(PollResponse, messages=())
            response = _service().poll(conn, request)
            return response.model_dump(mode="json")
        finally:
            conn.close()

    @mcp.tool(
        name="peer_ack",
        description=(
            "Acknowledge one peer message. Signature canonical: "
            "'peer_ack|<agent_id>|<participant_type>|<transport_type>|<client_ts>|<message_id>'."
        ),
    )
    def peer_ack(
        agent_id: str,
        participant_type: str,
        transport_type: str,
        client_ts: str,
        signature: str,
        message_id: str,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn,
                agent_id,
                client_ts,
                signature,
                ("peer_ack", agent_id, participant_type, transport_type, client_ts, message_id),
            )
            if err:
                return err
            try:
                request = AckRequest(
                    participant=_participant(agent_id, participant_type, transport_type),
                    message_id=message_id,
                )
            except ValidationError:
                return _invalid_payload_response(
                    AckResponse,
                    message_id=message_id,
                    acked_ts=None,
                )
            response = _service().ack(conn, request)
            return response.model_dump(mode="json")
        finally:
            conn.close()

    @mcp.tool(
        name="peer_get_thread",
        description=(
            "Return a peer thread transcript visible to the caller. Signature canonical: "
            "'peer_get_thread|<agent_id>|<participant_type>|<transport_type>|<client_ts>|<thread_id>'."
        ),
    )
    def peer_get_thread(
        agent_id: str,
        participant_type: str,
        transport_type: str,
        client_ts: str,
        signature: str,
        thread_id: str,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn,
                agent_id,
                client_ts,
                signature,
                (
                    "peer_get_thread",
                    agent_id,
                    participant_type,
                    transport_type,
                    client_ts,
                    thread_id,
                ),
            )
            if err:
                return err
            try:
                request = GetThreadRequest(
                    participant=_participant(agent_id, participant_type, transport_type),
                    thread_id=thread_id,
                )
            except ValidationError:
                return _invalid_payload_response(
                    GetThreadResponse,
                    thread_id=thread_id,
                    messages=(),
                )
            response = _service().get_thread(conn, request)
            return response.model_dump(mode="json")
        finally:
            conn.close()
