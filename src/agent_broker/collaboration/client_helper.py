"""Client-side signing helpers for collaboration MCP tools.

These helpers are convenience only. The broker independently verifies every
canonical field set and owns all approval/delivery decisions.
"""

from __future__ import annotations

from .. import database as db


def sign_collab_propose_message(
    secret: str,
    *,
    agent_id: str,
    participant_type: str,
    transport_type: str,
    client_ts: str,
    correlation_id: str,
    to_participants: list[str] | tuple[str, ...],
    message_kind: str,
    payload_json: dict,
    content_text: str,
    thread_id: str | None,
    subject: str | None,
) -> str:
    return db.compute_signature(
        secret,
        *db.build_collab_propose_signature_fields(
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


def sign_collab_poll(
    secret: str,
    *,
    agent_id: str,
    participant_type: str,
    transport_type: str,
    client_ts: str,
    limit: int,
) -> str:
    return db.compute_signature(
        secret,
        *db.build_collab_poll_signature_fields(
            agent_id=agent_id,
            participant_type=participant_type,
            transport_type=transport_type,
            timestamp=client_ts,
            limit=limit,
        ),
    )


def sign_collab_ack(
    secret: str,
    *,
    agent_id: str,
    participant_type: str,
    transport_type: str,
    client_ts: str,
    deliverable_id: str,
) -> str:
    return db.compute_signature(
        secret,
        *db.build_collab_ack_signature_fields(
            agent_id=agent_id,
            participant_type=participant_type,
            transport_type=transport_type,
            timestamp=client_ts,
            deliverable_id=deliverable_id,
        ),
    )


def sign_collab_get_thread(
    secret: str,
    *,
    agent_id: str,
    participant_type: str,
    transport_type: str,
    client_ts: str,
    thread_id: str,
) -> str:
    return db.compute_signature(
        secret,
        *db.build_collab_get_thread_signature_fields(
            agent_id=agent_id,
            participant_type=participant_type,
            transport_type=transport_type,
            timestamp=client_ts,
            thread_id=thread_id,
        ),
    )
