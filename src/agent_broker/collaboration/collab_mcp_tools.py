"""HMAC-authenticated MCP adapters for collaboration tools."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .. import database as db
from ..peer.peer_contracts import ParticipantRef
from .collab_contracts import (
    CollabAckRequest,
    CollabAckResponse,
    CollabGetThreadRequest,
    CollabGetThreadResponse,
    CollabPollRequest,
    CollabPollResponse,
    ProposeMessageRequest,
    ProposeMessageResponse,
    collab_error,
)
from .collab_service import CollaborationService
from .services import COLLABORATION_SERVICE, IDENTITY_SERVICE


def _invalid_payload_response(response_type, **fields) -> dict:
    return response_type(
        **fields,
        error=collab_error("invalid_payload", "collaboration MCP payload validation failed", None),
    ).model_dump(mode="json")


def register_collab_tools(mcp, _verify, _conn, AGENT_SECRETS) -> None:
    """Register generic collaboration MCP tools."""

    def _service() -> CollaborationService:
        return COLLABORATION_SERVICE

    def _participant(agent_id: str, participant_type: str, transport_type: str) -> ParticipantRef:
        resolved = IDENTITY_SERVICE.resolve(agent_id)
        return ParticipantRef(
            participant_id=agent_id,
            participant_type=participant_type,
            transport_type=transport_type,
            is_probe=bool(resolved and resolved.is_probe),
            collaboration_governed=bool(resolved and resolved.collaboration_governed),
        )

    @mcp.tool(
        name="collab_propose_message",
        description=(
            "Create a pending operator-mediated collaboration draft. Signature canonical: "
            "'collab_propose_message|<agent_id>|<participant_type>|<transport_type>|"
            "<client_ts>|<correlation_id>|<to_participants_json>|<message_kind>|"
            "<payload_json>|<content_text>|<thread_id_or_empty>|<subject_or_empty>'."
        ),
    )
    def collab_propose_message(
        agent_id: str,
        participant_type: str,
        transport_type: str,
        client_ts: str,
        signature: str,
        to_participants: list[str],
        message_kind: str,
        payload_json: dict[str, Any],
        content_text: str,
        correlation_id: str,
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
                db.build_collab_propose_signature_fields(
                    agent_id=agent_id,
                    participant_type=participant_type,
                    transport_type=transport_type,
                    timestamp=client_ts,
                    correlation_id=correlation_id,
                    to_participants=to_participants,
                    message_kind=message_kind,
                    payload_json=payload_json,
                    content_text=content_text,
                    thread_id=thread_id,
                    subject=subject,
                ),
            )
            if err:
                return err
            try:
                recipients = []
                for item in to_participants:
                    recipient = IDENTITY_SERVICE.resolve(item)
                    if recipient is None:
                        return ProposeMessageResponse(
                            draft=None,
                            error=collab_error(
                                "unknown_participant",
                                f"unknown recipient {item!r}",
                                {"participant_id": item},
                            ),
                        ).model_dump(mode="json")
                    recipients.append(recipient)
                request = ProposeMessageRequest(
                    from_participant=_participant(agent_id, participant_type, transport_type),
                    to_participants=tuple(recipients),
                    message_kind=message_kind,
                    payload_json=payload_json,
                    content_text=content_text,
                    correlation_id=correlation_id,
                    thread_id=thread_id,
                    subject=subject,
                )
            except ValidationError:
                return _invalid_payload_response(ProposeMessageResponse, draft=None)
            response = _service().propose(conn, request)
            return response.model_dump(mode="json")
        finally:
            conn.close()

    @mcp.tool(
        name="collab_poll",
        description=(
            "Poll approved collaboration deliverables addressed to the caller. Signature canonical: "
            "'collab_poll|<agent_id>|<participant_type>|<transport_type>|<client_ts>|<limit>'."
        ),
    )
    def collab_poll(
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
                db.build_collab_poll_signature_fields(
                    agent_id=agent_id,
                    participant_type=participant_type,
                    transport_type=transport_type,
                    timestamp=client_ts,
                    limit=limit,
                ),
            )
            if err:
                return err
            try:
                request = CollabPollRequest(
                    participant=_participant(agent_id, participant_type, transport_type),
                    limit=limit,
                )
            except ValidationError:
                return _invalid_payload_response(CollabPollResponse, deliverables=())
            response = _service().poll(conn, request)
            return response.model_dump(mode="json")
        finally:
            conn.close()

    @mcp.tool(
        name="collab_ack",
        description=(
            "Acknowledge a collaboration deliverable. Signature canonical: "
            "'collab_ack|<agent_id>|<participant_type>|<transport_type>|<client_ts>|"
            "<deliverable_id>'."
        ),
    )
    def collab_ack(
        agent_id: str,
        participant_type: str,
        transport_type: str,
        client_ts: str,
        signature: str,
        deliverable_id: str,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn,
                agent_id,
                client_ts,
                signature,
                db.build_collab_ack_signature_fields(
                    agent_id=agent_id,
                    participant_type=participant_type,
                    transport_type=transport_type,
                    timestamp=client_ts,
                    deliverable_id=deliverable_id,
                ),
            )
            if err:
                return err
            try:
                request = CollabAckRequest(
                    participant=_participant(agent_id, participant_type, transport_type),
                    deliverable_id=deliverable_id,
                )
            except ValidationError:
                return _invalid_payload_response(
                    CollabAckResponse,
                    deliverable_id=deliverable_id,
                    acked_ts=None,
                )
            response = _service().ack(conn, request)
            return response.model_dump(mode="json")
        finally:
            conn.close()

    @mcp.tool(
        name="collab_get_thread",
        description=(
            "Return collaboration thread entries visible to the caller. Signature canonical: "
            "'collab_get_thread|<agent_id>|<participant_type>|<transport_type>|<client_ts>|"
            "<thread_id>'."
        ),
    )
    def collab_get_thread(
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
                db.build_collab_get_thread_signature_fields(
                    agent_id=agent_id,
                    participant_type=participant_type,
                    transport_type=transport_type,
                    timestamp=client_ts,
                    thread_id=thread_id,
                ),
            )
            if err:
                return err
            try:
                request = CollabGetThreadRequest(
                    participant=_participant(agent_id, participant_type, transport_type),
                    thread_id=thread_id,
                )
            except ValidationError:
                return _invalid_payload_response(
                    CollabGetThreadResponse,
                    thread_id=thread_id,
                    entries=(),
                )
            response = _service().get_thread(conn, request)
            return response.model_dump(mode="json")
        finally:
            conn.close()
