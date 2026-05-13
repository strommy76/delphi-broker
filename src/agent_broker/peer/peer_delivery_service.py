"""
--------------------------------------------------------------------------------
FILE:        peer_delivery_service.py
PATH:        ~/projects/agent-broker/src/agent_broker/peer/peer_delivery_service.py
DESCRIPTION: Authority surface for peer send, poll, ack, and thread retrieval semantics.

CHANGELOG:
2026-05-06 16:15      Codex      [Fix] Add explicit recipient-authority gate to
                                      non-operator transcript reads.
2026-05-06 13:31      Codex      [Refactor] Remove unused audit-service dependency and make excluded threads permanently hidden.
2026-05-06 12:57      Codex      [Feature] Exclude probe and historically contaminated threads from operator transcript reads.
2026-05-06 11:25      Codex      [Feature] Add service-owned operator transcript reads and audit-only mark-read authority.
2026-05-06 11:14      Codex      [Fix] Make poll and ack audit writes atomic and batch receipt loading across message lists.
2026-05-06 09:47      Codex      [Refactor] Pre-validate parent ids, distinguish participant type mismatch, and use atomic store send.
2026-05-06 09:38      Codex      [Refactor] Remove falsey thread fallback and fail loud on blank explicit thread ids.
2026-05-06 09:35      Codex      [Feature] Add Phase 5 peer delivery service with redelivery, fanout, and idempotent ack behavior.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import json
from typing import Any

from . import peer_store
from .identity_service import IdentityService
from .peer_contracts import (
    AckRequest,
    AckResponse,
    GetThreadRequest,
    GetThreadResponse,
    ParticipantRef,
    PeerError,
    PeerMessage,
    PollRequest,
    PollResponse,
    SendRequest,
    SendResponse,
    peer_error,
)


class PeerDeliveryService:
    """Coordinate peer message delivery without owning DB or auth concerns."""

    def __init__(
        self,
        *,
        identity_service: IdentityService,
        operator_permanently_hidden_thread_ids: frozenset[str],
    ) -> None:
        self._identity = identity_service
        self._operator_permanently_hidden_thread_ids = operator_permanently_hidden_thread_ids

    def send(self, conn, request: SendRequest) -> SendResponse:
        sender, sender_error = self._resolve(request.from_participant)
        if sender_error is not None:
            return SendResponse(message=None, error=sender_error)
        if sender is None:
            raise RuntimeError("participant resolution returned no participant and no error")
        if sender.collaboration_governed:
            return SendResponse(
                message=None,
                error=peer_error(
                    "collaboration_required",
                    "participant is governed for operator-mediated collaboration",
                    {"participant_id": sender.participant_id},
                ),
            )

        recipients = self._resolve_recipients(request)
        if isinstance(recipients, SendResponse):
            return recipients
        governed_recipients = [
            recipient.participant_id for recipient in recipients if recipient.collaboration_governed
        ]
        if governed_recipients:
            return SendResponse(
                message=None,
                error=peer_error(
                    "collaboration_required",
                    "recipient is governed for operator-mediated collaboration",
                    {"participant_ids": governed_recipients},
                ),
            )
        if any(item.participant_id == sender.participant_id for item in recipients):
            # Atomic fail-loud fanout semantics: a self-recipient anywhere in
            # the recipient set rejects the entire send instead of silently
            # filtering caller intent.
            return SendResponse(
                message=None,
                error=peer_error(
                    "forbidden_recipient",
                    "self messaging is not allowed",
                    {"participant_id": sender.participant_id},
                ),
            )

        parent_error = self._validate_parent(conn, request)
        if parent_error is not None:
            return SendResponse(message=None, error=parent_error)

        sent_ts = peer_store.utc_now()
        thread_id, create_thread_args, thread_error = self._thread_context(conn, request, sent_ts)
        if thread_error is not None:
            return SendResponse(message=None, error=thread_error)
        if thread_id is None:
            raise RuntimeError("thread resolution returned no thread and no error")

        message_id = peer_store.new_id()
        message = peer_store.send_message(
            conn,
            create_thread_args=create_thread_args,
            message_args={
                "message_id": message_id,
                "thread_id": thread_id,
                "from_participant": sender.participant_id,
                "from_participant_type": sender.participant_type,
                "from_transport_type": sender.transport_type,
                "kind": request.message_kind,
                "payload_json": request.payload_json,
                "content_text": request.content_text,
                "correlation_id": request.correlation_id,
                "parent_message_id": request.parent_message_id,
                "sent_ts": sent_ts,
            },
            receipt_args=[
                {
                    "message_id": message_id,
                    "recipient_participant": recipient.participant_id,
                    "recipient_type": recipient.participant_type,
                    "recipient_transport": recipient.transport_type,
                    "recipient_order": index,
                }
                for index, recipient in enumerate(recipients)
            ],
            event_args={
                "event_id": peer_store.new_id(),
                "message_id": message_id,
                "participant_id": sender.participant_id,
                "event_kind": "message_sent",
                "event_ts": sent_ts,
                "detail_json": {"recipient_count": len(recipients)},
            },
        )
        return SendResponse(message=self._message_from_row(conn, message), error=None)

    def poll(self, conn, request: PollRequest) -> PollResponse:
        participant, error = self._resolve(request.participant)
        if error is not None:
            return PollResponse(messages=(), error=error)
        if participant is None:
            raise RuntimeError("participant resolution returned no participant and no error")

        message_rows = []
        delivered_ts = peer_store.utc_now()
        for row in peer_store.list_unacked_for_recipient(
            conn,
            participant.participant_id,
            limit=request.limit,
        ):
            message_rows.append(
                peer_store.poll_one_message(
                    conn,
                    message_id=row["message_id"],
                    recipient_participant=participant.participant_id,
                    delivered_ts=delivered_ts,
                    event_args={
                        "event_id": peer_store.new_id(),
                        "message_id": row["message_id"],
                        "participant_id": participant.participant_id,
                        "event_kind": "message_polled",
                        "event_ts": peer_store.utc_now(),
                        "detail_json": {"recipient": participant.participant_id},
                    },
                )
            )
        messages = self._messages_from_rows(conn, message_rows)
        return PollResponse(messages=messages, error=None)

    def ack(self, conn, request: AckRequest) -> AckResponse:
        participant, error = self._resolve(request.participant)
        if error is not None:
            return AckResponse(message_id=request.message_id, acked_ts=None, error=error)
        if participant is None:
            raise RuntimeError("participant resolution returned no participant and no error")

        if peer_store.get_message(conn, request.message_id) is None:
            return AckResponse(
                message_id=request.message_id,
                acked_ts=None,
                error=peer_error(
                    "message_not_found",
                    f"message {request.message_id!r} not found",
                    {"message_id": request.message_id},
                ),
            )
        if peer_store.get_receipt(conn, request.message_id, participant.participant_id) is None:
            return AckResponse(
                message_id=request.message_id,
                acked_ts=None,
                error=peer_error(
                    "forbidden_recipient",
                    "message is not addressed to participant",
                    {
                        "message_id": request.message_id,
                        "participant_id": participant.participant_id,
                    },
                ),
            )
        acked_ts = peer_store.utc_now()
        receipt, changed = peer_store.ack_message(
            conn,
            message_id=request.message_id,
            recipient_participant=participant.participant_id,
            acked_ts=acked_ts,
            event_args={
                "event_id": peer_store.new_id(),
                "message_id": request.message_id,
                "participant_id": participant.participant_id,
                "event_kind": "message_acked",
                "event_ts": acked_ts,
                "detail_json": {"message_id": request.message_id},
            },
        )
        if not changed:
            return AckResponse(
                message_id=request.message_id,
                acked_ts=receipt["acked_ts"],
                error=peer_error(
                    "ack_idempotent",
                    "message was already acknowledged",
                    {"message_id": request.message_id},
                ),
            )
        return AckResponse(message_id=request.message_id, acked_ts=receipt["acked_ts"], error=None)

    def get_thread(self, conn, request: GetThreadRequest) -> GetThreadResponse:
        participant, error = self._resolve(request.participant)
        if error is not None:
            return GetThreadResponse(thread_id=request.thread_id, messages=(), error=error)
        if participant is None:
            raise RuntimeError("participant resolution returned no participant and no error")

        if peer_store.get_thread(conn, request.thread_id) is None:
            return GetThreadResponse(
                thread_id=request.thread_id,
                messages=(),
                error=peer_error(
                    "message_not_found",
                    f"thread {request.thread_id!r} not found",
                    {"thread_id": request.thread_id},
                ),
            )
        messages = self._messages_from_rows(
            conn,
            peer_store.list_thread_messages(conn, request.thread_id),
        )
        if messages and not self._participant_can_read_messages(
            messages,
            participant.participant_id,
        ):
            return GetThreadResponse(
                thread_id=request.thread_id,
                messages=(),
                error=peer_error(
                    "forbidden_recipient",
                    "participant is not part of this thread",
                    {
                        "thread_id": request.thread_id,
                        "participant_id": participant.participant_id,
                    },
                ),
            )
        return GetThreadResponse(thread_id=request.thread_id, messages=messages, error=None)

    def list_threads(
        self,
        conn,
        *,
        limit: int,
        offset: int,
        include_probes: bool,
    ) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        rows = peer_store.list_threads(conn)
        summaries = []
        for row in rows:
            messages = peer_store.list_thread_messages(conn, row["thread_id"])
            receipt_map = peer_store.list_receipts_for_messages(
                conn,
                (message["message_id"] for message in messages),
            )
            if not self._operator_thread_visible(
                row["thread_id"],
                messages,
                receipt_map,
                include_probes=include_probes,
            ):
                continue
            participants = {message["from_participant"] for message in messages}
            for receipts in receipt_map.values():
                participants.update(receipt["recipient_participant"] for receipt in receipts)
            summaries.append(
                {
                    "thread_id": row["thread_id"],
                    "subject": row["subject"],
                    "status": row["status"],
                    "created_ts": row["created_ts"],
                    "last_activity_ts": row["last_activity_ts"],
                    "message_count": row["message_count"],
                    "participants": tuple(sorted(participants)),
                }
            )
        return {
            "threads": tuple(summaries[offset : offset + limit]),
            "limit": limit,
            "offset": offset,
            "include_probes": include_probes,
        }

    def get_thread_transcript(
        self,
        conn,
        thread_id: str,
        *,
        include_probes: bool,
        requires_recipient_check: bool = True,
        participant: ParticipantRef | None = None,
    ) -> dict[str, Any]:
        thread = peer_store.get_thread(conn, thread_id)
        if thread is None:
            raise ValueError(f"thread {thread_id!r} not found")
        message_rows = peer_store.list_thread_messages(conn, thread_id)
        receipt_map = peer_store.list_receipts_for_messages(
            conn,
            (row["message_id"] for row in message_rows),
        )
        if not self._operator_thread_visible(
            thread_id,
            message_rows,
            receipt_map,
            include_probes=include_probes,
        ):
            raise ValueError(f"thread {thread_id!r} not found")
        event_map = peer_store.list_events_for_messages(
            conn,
            (row["message_id"] for row in message_rows),
        )
        messages = self._messages_from_rows(conn, message_rows, receipt_map=receipt_map)
        if requires_recipient_check:
            if participant is None:
                raise PermissionError("participant is required for transcript recipient checks")
            resolved, error = self._resolve(participant)
            if error is not None:
                raise PermissionError(error.message)
            if resolved is None:
                raise RuntimeError("participant resolution returned no participant and no error")
            if not self._participant_can_read_messages(messages, resolved.participant_id):
                raise PermissionError("participant is not part of this thread")
        return {
            "thread": {
                "thread_id": thread["thread_id"],
                "subject": thread["subject"],
                "status": thread["status"],
                "created_ts": thread["created_ts"],
            },
            "messages": tuple(
                self._message_detail(
                    conn,
                    message,
                    receipts=receipt_map.get(message.message_id, []),
                    events=event_map.get(message.message_id, []),
                )
                for message in messages
            ),
        }

    def get_message_detail(
        self,
        conn,
        message_id: str,
        *,
        include_probes: bool,
    ) -> dict[str, Any]:
        row = peer_store.get_message(conn, message_id)
        if row is None:
            raise ValueError(f"message {message_id!r} not found")
        thread = peer_store.get_message_thread(conn, message_id)
        if thread is None:
            raise ValueError(f"thread for message {message_id!r} not found")
        receipts = peer_store.list_receipts_for_message(conn, message_id)
        if not self._operator_thread_visible(
            thread["thread_id"],
            [row],
            {message_id: receipts},
            include_probes=include_probes,
        ):
            raise ValueError(f"message {message_id!r} not found")
        message = self._message_from_row(conn, row)
        detail = self._message_detail(conn, message)
        detail["thread"] = thread
        return detail

    def mark_read(self, conn, *, message_id: str, recipient_participant: str) -> dict[str, Any]:
        if not recipient_participant.strip():
            raise ValueError("recipient_participant must not be blank")
        if peer_store.get_message(conn, message_id) is None:
            raise LookupError(f"message {message_id!r} not found")
        if peer_store.get_receipt(conn, message_id, recipient_participant) is None:
            raise PermissionError("message is not addressed to recipient")
        event_ts = peer_store.utc_now()
        receipt = peer_store.mark_message_read(
            conn,
            message_id=message_id,
            recipient_participant=recipient_participant,
            event_args={
                "event_id": peer_store.new_id(),
                "message_id": message_id,
                "participant_id": recipient_participant,
                "event_kind": "message_read",
                "event_ts": event_ts,
                "detail_json": {"recipient": recipient_participant},
            },
        )
        return {
            "message_id": message_id,
            "recipient_participant": recipient_participant,
            "read_ts": event_ts,
            "receipt": receipt,
        }

    def _validate_parent(self, conn, request: SendRequest) -> PeerError | None:
        if request.parent_message_id is None:
            return None
        parent_message_id = request.parent_message_id.strip()
        if not parent_message_id:
            return peer_error("invalid_payload", "parent_message_id must not be blank", None)
        if peer_store.get_message(conn, parent_message_id) is None:
            return peer_error(
                "invalid_payload",
                "parent_message_id does not exist",
                {"parent_message_id": request.parent_message_id},
            )
        return None

    def _thread_context(
        self,
        conn,
        request: SendRequest,
        sent_ts: str,
    ) -> tuple[str | None, dict[str, Any] | None, PeerError | None]:
        if request.thread_id is None:
            subject = (request.subject or "").strip()
            if not subject:
                return (
                    None,
                    None,
                    peer_error(
                        "invalid_payload",
                        "subject is required when creating a new thread",
                        None,
                    ),
                )
            thread_id = peer_store.new_id()
            return (
                thread_id,
                {"thread_id": thread_id, "subject": subject, "created_ts": sent_ts},
                None,
            )

        thread_id = request.thread_id.strip()
        if not thread_id:
            return None, None, peer_error("invalid_payload", "thread_id must not be blank", None)
        if peer_store.get_thread(conn, thread_id) is None:
            return (
                None,
                None,
                peer_error(
                    "invalid_payload",
                    f"unknown thread_id {request.thread_id!r}",
                    {"thread_id": request.thread_id},
                ),
            )
        return thread_id, None, None

    def _resolve(
        self, participant: ParticipantRef
    ) -> tuple[ParticipantRef | None, PeerError | None]:
        resolved = self._identity.resolve(participant.participant_id)
        if resolved is None:
            return (
                None,
                peer_error(
                    "unknown_participant",
                    f"unknown participant {participant.participant_id!r}",
                    {"participant_id": participant.participant_id},
                ),
            )
        if (
            resolved.participant_type != participant.participant_type
            or resolved.transport_type != participant.transport_type
        ):
            return (
                None,
                peer_error(
                    "participant_type_mismatch",
                    "participant identity shape does not match registry",
                    {
                        "participant_id": participant.participant_id,
                        "registry_type": resolved.participant_type,
                        "claimed_type": participant.participant_type,
                        "registry_transport": resolved.transport_type,
                        "claimed_transport": participant.transport_type,
                    },
                ),
            )
        return resolved, None

    def _resolve_recipients(
        self,
        request: SendRequest,
    ) -> tuple[ParticipantRef, ...] | SendResponse:
        if request.to_participants is None:
            recipients = tuple(
                item
                for item in self._identity.all_participants()
                if item.participant_id != request.from_participant.participant_id
            )
            if not recipients:
                return SendResponse(
                    message=None,
                    error=peer_error("forbidden_recipient", "broadcast has no recipients", None),
                )
            return recipients
        if not request.to_participants:
            return SendResponse(
                message=None,
                error=peer_error("forbidden_recipient", "recipient list is empty", None),
            )
        resolved: list[ParticipantRef] = []
        for recipient in request.to_participants:
            participant, error = self._resolve(recipient)
            if error is not None:
                return SendResponse(message=None, error=error)
            if participant is None:
                raise RuntimeError("participant resolution returned no participant and no error")
            resolved.append(participant)
        return tuple(resolved)

    def _messages_from_rows(
        self,
        conn,
        rows: list[dict[str, Any]],
        *,
        receipt_map: dict[str, list[dict[str, Any]]] | None = None,
    ) -> tuple[PeerMessage, ...]:
        receipts_by_message = (
            receipt_map
            if receipt_map is not None
            else peer_store.list_receipts_for_messages(
                conn,
                (row["message_id"] for row in rows),
            )
        )
        return tuple(
            self._message_from_row(conn, row, receipts_by_message.get(row["message_id"], []))
            for row in rows
        )

    def _message_from_row(
        self,
        conn,
        row: dict[str, Any],
        receipts: list[dict[str, Any]] | None = None,
    ) -> PeerMessage:
        message_receipts = (
            receipts
            if receipts is not None
            else peer_store.list_receipts_for_message(conn, row["message_id"])
        )
        recipients = tuple(
            ParticipantRef(
                participant_id=receipt["recipient_participant"],
                participant_type=receipt["recipient_type"],
                transport_type=receipt["recipient_transport"],
                is_probe=self._identity.is_probe(receipt["recipient_participant"]),
            )
            for receipt in message_receipts
        )
        return PeerMessage(
            message_id=row["message_id"],
            thread_id=row["thread_id"],
            from_participant=ParticipantRef(
                participant_id=row["from_participant"],
                participant_type=row["from_participant_type"],
                transport_type=row["from_transport_type"],
                is_probe=self._identity.is_probe(row["from_participant"]),
            ),
            to_participants=recipients,
            message_kind=row["kind"],
            payload_json=self._payload(row),
            content_text=row["content_text"],
            correlation_id=row["correlation_id"],
            parent_message_id=row["parent_message_id"],
            sent_ts=row["sent_ts"],
        )

    def _payload(self, row: dict[str, Any]) -> dict[str, Any]:
        value = json.loads(row["payload_json"])
        if not isinstance(value, dict):
            raise ValueError(f"message {row['message_id']!r} payload_json is not an object")
        return value

    def _message_detail(
        self,
        conn,
        message: PeerMessage,
        *,
        receipts: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        message_receipts = (
            receipts
            if receipts is not None
            else peer_store.list_receipts_for_message(conn, message.message_id)
        )
        message_events = (
            events
            if events is not None
            else peer_store.list_events_for_message(conn, message.message_id)
        )
        return {
            "message": message.model_dump(mode="json"),
            "receipts": tuple(message_receipts),
            "events": tuple(
                {
                    **event,
                    "detail_json": json.loads(event["detail_json"]),
                }
                for event in message_events
            ),
        }

    def _operator_thread_visible(
        self,
        thread_id: str,
        message_rows: list[dict[str, Any]],
        receipt_map: dict[str, list[dict[str, Any]]],
        *,
        include_probes: bool,
    ) -> bool:
        if thread_id in self._operator_permanently_hidden_thread_ids:
            return False
        if include_probes:
            return True
        for message in message_rows:
            if self._identity.is_probe(message["from_participant"]):
                return False
            for receipt in receipt_map.get(message["message_id"], []):
                if self._identity.is_probe(receipt["recipient_participant"]):
                    return False
        return True

    def _participant_can_read_messages(
        self,
        messages: tuple[PeerMessage, ...],
        participant_id: str,
    ) -> bool:
        return any(
            message.from_participant.participant_id == participant_id
            or any(
                recipient.participant_id == participant_id
                for recipient in (message.to_participants or ())
            )
            for message in messages
        )
