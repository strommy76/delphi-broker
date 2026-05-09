"""
Service seam for operator-mediated collaboration decisions and delivery.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..peer.identity_service import IdentityService
from ..peer.peer_contracts import ParticipantRef
from . import collab_store
from .collab_contracts import (
    CollabAckRequest,
    CollabAckResponse,
    CollabDecision,
    CollabDeliverable,
    CollabDraft,
    CollabError,
    CollabGetThreadRequest,
    CollabGetThreadResponse,
    CollabPollRequest,
    CollabPollResponse,
    OperatorDecisionRequest,
    OperatorDecisionResponse,
    ProposeMessageRequest,
    ProposeMessageResponse,
    collab_error,
)


class CollaborationService:
    """Own operator-mediated collaboration authority."""

    def __init__(
        self,
        *,
        identity_service: IdentityService,
        operator_participant_id: str,
    ) -> None:
        self._identity = identity_service
        self._operator_participant_id = operator_participant_id

    def propose(
        self, conn: sqlite3.Connection, request: ProposeMessageRequest
    ) -> ProposeMessageResponse:
        sender, sender_error = self._resolve(request.from_participant)
        if sender_error is not None:
            return ProposeMessageResponse(draft=None, error=sender_error)
        if sender is None:
            raise RuntimeError("participant resolution returned no participant and no error")
        if not sender.collaboration_governed:
            return ProposeMessageResponse(
                draft=None,
                error=collab_error(
                    "collaboration_required",
                    "participant is not governed for operator-mediated collaboration",
                    {"participant_id": sender.participant_id},
                ),
            )
        recipients, recipient_error = self._resolve_recipients(request.to_participants)
        if recipient_error is not None:
            return ProposeMessageResponse(draft=None, error=recipient_error)
        if any(item.participant_id == sender.participant_id for item in recipients):
            return ProposeMessageResponse(
                draft=None,
                error=collab_error(
                    "forbidden_recipient",
                    "self messaging is not allowed",
                    {"participant_id": sender.participant_id},
                ),
            )

        existing = collab_store.get_draft_by_idempotency(
            conn,
            from_participant=sender.participant_id,
            correlation_id=request.correlation_id,
        )
        if existing is not None:
            if not self._draft_matches_request(conn, existing, request, recipients):
                return ProposeMessageResponse(
                    draft=None,
                    error=collab_error(
                        "idempotency_conflict",
                        "correlation_id already used for different draft payload",
                        {
                            "participant_id": sender.participant_id,
                            "correlation_id": request.correlation_id,
                        },
                    ),
                )
            return ProposeMessageResponse(draft=self._draft_from_row(conn, existing), error=None)

        created_ts = collab_store.utc_now()
        thread_id, create_thread_args, thread_error = self._thread_context(
            conn,
            request,
            created_ts,
        )
        if thread_error is not None:
            return ProposeMessageResponse(draft=None, error=thread_error)
        if thread_id is None:
            raise RuntimeError("thread resolution returned no thread and no error")

        draft_id = collab_store.new_id()
        draft = collab_store.create_draft(
            conn,
            create_thread_args=create_thread_args,
            draft_args={
                "draft_id": draft_id,
                "thread_id": thread_id,
                "from_participant": sender.participant_id,
                "from_participant_type": sender.participant_type,
                "from_transport_type": sender.transport_type,
                "kind": request.message_kind,
                "payload_json": request.payload_json,
                "content_text": request.content_text,
                "correlation_id": request.correlation_id,
                "created_ts": created_ts,
            },
            recipient_args=[
                {
                    "draft_id": draft_id,
                    "recipient_participant": recipient.participant_id,
                    "recipient_type": recipient.participant_type,
                    "recipient_transport": recipient.transport_type,
                    "recipient_order": index,
                }
                for index, recipient in enumerate(recipients)
            ],
            event_args={
                "event_id": collab_store.new_id(),
                "draft_id": draft_id,
                "decision_id": None,
                "deliverable_id": None,
                "participant_id": sender.participant_id,
                "event_kind": "draft_created",
                "event_ts": created_ts,
                "detail_json": {"recipient_count": len(recipients)},
            },
        )
        return ProposeMessageResponse(draft=self._draft_from_row(conn, draft), error=None)

    def decide(
        self,
        conn: sqlite3.Connection,
        request: OperatorDecisionRequest,
    ) -> OperatorDecisionResponse:
        operator, operator_error = self._resolve(request.operator_participant)
        if operator_error is not None:
            return OperatorDecisionResponse(decision=None, deliverable=None, error=operator_error)
        if operator is None:
            raise RuntimeError("participant resolution returned no participant and no error")
        if (
            operator.participant_id != self._operator_participant_id
            or operator.participant_type != "operator"
        ):
            return OperatorDecisionResponse(
                decision=None,
                deliverable=None,
                error=collab_error(
                    "forbidden_participant",
                    "operator decision must come from configured operator participant",
                    {"participant_id": operator.participant_id},
                ),
            )

        draft = collab_store.get_draft(conn, request.draft_id)
        if draft is None:
            return OperatorDecisionResponse(
                decision=None,
                deliverable=None,
                error=collab_error(
                    "draft_not_found",
                    f"draft {request.draft_id!r} not found",
                    {"draft_id": request.draft_id},
                ),
            )
        existing = collab_store.get_decision_for_draft(conn, request.draft_id)
        normalized, normalized_error = self._normalize_decision(conn, draft, request)
        if normalized_error is not None:
            return OperatorDecisionResponse(decision=None, deliverable=None, error=normalized_error)
        if existing is not None:
            if not self._decision_matches(conn, existing, normalized, request):
                return OperatorDecisionResponse(
                    decision=None,
                    deliverable=None,
                    error=collab_error(
                        "idempotency_conflict",
                        "draft already has a different operator decision",
                        {"draft_id": request.draft_id},
                    ),
                )
            return OperatorDecisionResponse(
                decision=self._decision_from_row(existing),
                deliverable=self._deliverable_from_row(
                    conn,
                    collab_store.get_deliverable_for_decision(conn, existing["decision_id"]),
                ),
                error=None,
            )

        decision_ts = collab_store.utc_now()
        decision_id = collab_store.new_id()
        deliverable_id = collab_store.new_id() if normalized["deliverable"] else None
        decision, deliverable = collab_store.record_decision(
            conn,
            decision_args={
                "decision_id": decision_id,
                "draft_id": draft["draft_id"],
                "operator_participant": operator.participant_id,
                "decision_type": request.decision_type,
                "final_payload_json": normalized["payload_json"],
                "final_content_text": normalized["content_text"],
                "reason": request.reason,
                "decision_ts": decision_ts,
            },
            decision_recipient_args=[
                {
                    "decision_id": decision_id,
                    "recipient_participant": recipient.participant_id,
                    "recipient_type": recipient.participant_type,
                    "recipient_transport": recipient.transport_type,
                    "recipient_order": index,
                }
                for index, recipient in enumerate(normalized["recipients"])
            ],
            deliverable_args=(
                None
                if deliverable_id is None
                else {
                    "deliverable_id": deliverable_id,
                    "draft_id": draft["draft_id"],
                    "decision_id": decision_id,
                    "thread_id": draft["thread_id"],
                    "from_participant": draft["from_participant"],
                    "from_participant_type": draft["from_participant_type"],
                    "from_transport_type": draft["from_transport_type"],
                    "kind": draft["kind"],
                    "payload_json": normalized["payload_json"],
                    "content_text": normalized["content_text"],
                    "correlation_id": draft["correlation_id"],
                    "created_ts": decision_ts,
                }
            ),
            receipt_args=[
                {
                    "deliverable_id": deliverable_id,
                    "recipient_participant": recipient.participant_id,
                    "recipient_type": recipient.participant_type,
                    "recipient_transport": recipient.transport_type,
                    "recipient_order": index,
                }
                for index, recipient in enumerate(normalized["recipients"])
                if deliverable_id is not None
            ],
            decision_event_args={
                "event_id": collab_store.new_id(),
                "draft_id": draft["draft_id"],
                "decision_id": decision_id,
                "deliverable_id": None,
                "participant_id": operator.participant_id,
                "event_kind": f"operator_{request.decision_type}",
                "event_ts": decision_ts,
                "detail_json": {"reason": request.reason},
            },
            deliverable_event_args=(
                None
                if deliverable_id is None
                else {
                    "event_id": collab_store.new_id(),
                    "draft_id": draft["draft_id"],
                    "decision_id": decision_id,
                    "deliverable_id": deliverable_id,
                    "participant_id": operator.participant_id,
                    "event_kind": "deliverable_created",
                    "event_ts": decision_ts,
                    "detail_json": {"recipient_count": len(normalized["recipients"])},
                }
            ),
        )
        return OperatorDecisionResponse(
            decision=self._decision_from_row(decision),
            deliverable=self._deliverable_from_row(conn, deliverable),
            error=None,
        )

    def poll(self, conn: sqlite3.Connection, request: CollabPollRequest) -> CollabPollResponse:
        participant, error = self._resolve_collaboration_participant(request.participant)
        if error is not None:
            return CollabPollResponse(deliverables=(), error=error)
        if participant is None:
            raise RuntimeError("participant resolution returned no participant and no error")

        delivered_ts = collab_store.utc_now()
        rows = []
        for row in collab_store.list_unacked_for_recipient(
            conn,
            participant.participant_id,
            limit=request.limit,
        ):
            rows.append(
                collab_store.mark_delivered(
                    conn,
                    deliverable_id=row["deliverable_id"],
                    recipient_participant=participant.participant_id,
                    delivered_ts=delivered_ts,
                    event_args={
                        "event_id": collab_store.new_id(),
                        "draft_id": row["draft_id"],
                        "decision_id": row["decision_id"],
                        "deliverable_id": row["deliverable_id"],
                        "participant_id": participant.participant_id,
                        "event_kind": "deliverable_polled",
                        "event_ts": collab_store.utc_now(),
                        "detail_json": {"recipient": participant.participant_id},
                    },
                )
            )
        return CollabPollResponse(
            deliverables=tuple(self._deliverable_from_row(conn, row) for row in rows),
            error=None,
        )

    def ack(self, conn: sqlite3.Connection, request: CollabAckRequest) -> CollabAckResponse:
        participant, error = self._resolve_collaboration_participant(request.participant)
        if error is not None:
            return CollabAckResponse(
                deliverable_id=request.deliverable_id,
                acked_ts=None,
                error=error,
            )
        if participant is None:
            raise RuntimeError("participant resolution returned no participant and no error")
        deliverable = collab_store.get_deliverable(conn, request.deliverable_id)
        if deliverable is None:
            return CollabAckResponse(
                deliverable_id=request.deliverable_id,
                acked_ts=None,
                error=collab_error(
                    "deliverable_not_found",
                    f"deliverable {request.deliverable_id!r} not found",
                    {"deliverable_id": request.deliverable_id},
                ),
            )
        if (
            collab_store.get_receipt(conn, request.deliverable_id, participant.participant_id)
            is None
        ):
            return CollabAckResponse(
                deliverable_id=request.deliverable_id,
                acked_ts=None,
                error=collab_error(
                    "forbidden_recipient",
                    "deliverable is not addressed to participant",
                    {
                        "deliverable_id": request.deliverable_id,
                        "participant_id": participant.participant_id,
                    },
                ),
            )
        receipt = collab_store.get_receipt(conn, request.deliverable_id, participant.participant_id)
        if receipt is None:
            raise RuntimeError("receipt lookup returned no row after existence check")
        if receipt["delivered_ts"] is None:
            return CollabAckResponse(
                deliverable_id=request.deliverable_id,
                acked_ts=None,
                error=collab_error(
                    "delivery_required",
                    "deliverable must be delivered before it can be acknowledged",
                    {"deliverable_id": request.deliverable_id},
                ),
            )
        acked_ts = collab_store.utc_now()
        receipt, changed = collab_store.mark_acked(
            conn,
            deliverable_id=request.deliverable_id,
            recipient_participant=participant.participant_id,
            acked_ts=acked_ts,
            event_args={
                "event_id": collab_store.new_id(),
                "draft_id": deliverable["draft_id"],
                "decision_id": deliverable["decision_id"],
                "deliverable_id": request.deliverable_id,
                "participant_id": participant.participant_id,
                "event_kind": "deliverable_acked",
                "event_ts": acked_ts,
                "detail_json": {"deliverable_id": request.deliverable_id},
            },
        )
        if not changed:
            return CollabAckResponse(
                deliverable_id=request.deliverable_id,
                acked_ts=receipt["acked_ts"],
                error=collab_error(
                    "ack_idempotent",
                    "deliverable was already acknowledged",
                    {"deliverable_id": request.deliverable_id},
                ),
            )
        return CollabAckResponse(
            deliverable_id=request.deliverable_id,
            acked_ts=receipt["acked_ts"],
            error=None,
        )

    def get_thread(
        self,
        conn: sqlite3.Connection,
        request: CollabGetThreadRequest,
    ) -> CollabGetThreadResponse:
        participant, error = self._resolve(request.participant)
        if error is not None:
            return CollabGetThreadResponse(thread_id=request.thread_id, entries=(), error=error)
        if participant is None:
            raise RuntimeError("participant resolution returned no participant and no error")
        if collab_store.get_thread(conn, request.thread_id) is None:
            return CollabGetThreadResponse(
                thread_id=request.thread_id,
                entries=(),
                error=collab_error(
                    "thread_not_found",
                    f"thread {request.thread_id!r} not found",
                    {"thread_id": request.thread_id},
                ),
            )
        if not self._participant_can_read_thread(conn, request.thread_id, participant):
            return CollabGetThreadResponse(
                thread_id=request.thread_id,
                entries=(),
                error=collab_error(
                    "thread_not_found",
                    "thread not found or not visible",
                    {"thread_id": request.thread_id},
                ),
            )
        return CollabGetThreadResponse(
            thread_id=request.thread_id,
            entries=tuple(self._thread_entries(conn, request.thread_id, participant)),
            error=None,
        )

    def list_pending_drafts(
        self,
        conn: sqlite3.Connection,
        *,
        include_probes: bool,
    ) -> dict[str, Any]:
        drafts = []
        for row in collab_store.list_pending_drafts(conn, include_probes=include_probes):
            draft = self._draft_from_row(conn, row)
            if not include_probes and self._draft_has_probe_participant(draft):
                continue
            drafts.append(draft.model_dump(mode="json"))
        return {"drafts": drafts}

    def _resolve(
        self, participant: ParticipantRef
    ) -> tuple[ParticipantRef | None, CollabError | None]:
        resolved = self._identity.resolve(participant.participant_id)
        if resolved is None:
            return None, collab_error(
                "unknown_participant",
                f"unknown participant {participant.participant_id!r}",
                {"participant_id": participant.participant_id},
            )
        if (
            resolved.participant_type != participant.participant_type
            or resolved.transport_type != participant.transport_type
        ):
            return None, collab_error(
                "forbidden_participant",
                "participant type or transport type mismatch",
                {
                    "participant_id": participant.participant_id,
                    "expected_participant_type": resolved.participant_type,
                    "expected_transport_type": resolved.transport_type,
                },
            )
        return resolved, None

    def _resolve_collaboration_participant(
        self,
        participant: ParticipantRef,
    ) -> tuple[ParticipantRef | None, CollabError | None]:
        resolved, error = self._resolve(participant)
        if error is not None:
            return None, error
        if resolved is None:
            raise RuntimeError("participant resolution returned no participant and no error")
        if not resolved.collaboration_governed:
            return None, collab_error(
                "collaboration_required",
                "participant is not governed for operator-mediated collaboration",
                {"participant_id": resolved.participant_id},
            )
        return resolved, None

    def _resolve_recipients(
        self,
        recipients: tuple[ParticipantRef, ...],
    ) -> tuple[tuple[ParticipantRef, ...], CollabError | None]:
        resolved_recipients = []
        for recipient in recipients:
            resolved, error = self._resolve_collaboration_participant(recipient)
            if error is not None:
                return (), error
            if resolved is None:
                raise RuntimeError("participant resolution returned no participant and no error")
            resolved_recipients.append(resolved)
        if not resolved_recipients:
            return (), collab_error("forbidden_recipient", "recipient list is empty", None)
        return tuple(resolved_recipients), None

    def _thread_context(
        self,
        conn: sqlite3.Connection,
        request: ProposeMessageRequest,
        created_ts: str,
    ) -> tuple[str | None, dict[str, Any] | None, CollabError | None]:
        if request.thread_id is None:
            subject = (request.subject or "").strip()
            if not subject:
                return (
                    None,
                    None,
                    collab_error(
                        "invalid_payload",
                        "subject is required when creating a new thread",
                        None,
                    ),
                )
            thread_id = collab_store.new_id()
            return (
                thread_id,
                {"thread_id": thread_id, "subject": subject, "created_ts": created_ts},
                None,
            )
        thread_id = request.thread_id.strip()
        if not thread_id:
            return None, None, collab_error("invalid_payload", "thread_id must not be blank", None)
        if collab_store.get_thread(conn, thread_id) is None:
            return (
                None,
                None,
                collab_error(
                    "thread_not_found",
                    f"unknown thread_id {request.thread_id!r}",
                    {"thread_id": request.thread_id},
                ),
            )
        return thread_id, None, None

    def _draft_matches_request(
        self,
        conn: sqlite3.Connection,
        draft: dict[str, Any],
        request: ProposeMessageRequest,
        recipients: tuple[ParticipantRef, ...],
    ) -> bool:
        return (
            draft["kind"] == request.message_kind
            and collab_store.payload_from_row(draft) == request.payload_json
            and draft["content_text"] == request.content_text
            and self._recipient_ids(collab_store.list_draft_recipients(conn, draft["draft_id"]))
            == tuple(item.participant_id for item in recipients)
        )

    def _normalize_decision(
        self,
        conn: sqlite3.Connection,
        draft: dict[str, Any],
        request: OperatorDecisionRequest,
    ) -> tuple[dict[str, Any], CollabError | None]:
        draft_payload = collab_store.payload_from_row(draft)
        draft_recipients = self._participants_from_rows(
            collab_store.list_draft_recipients(conn, draft["draft_id"])
        )
        if request.decision_type == "reject":
            if (
                request.final_payload_json is not None
                or request.final_content_text is not None
                or request.to_participants is not None
            ):
                return {}, collab_error(
                    "invalid_payload",
                    "reject cannot carry final content, final payload, or recipients",
                    None,
                )
            return {
                "payload_json": None,
                "content_text": None,
                "recipients": (),
                "deliverable": False,
            }, None
        if request.decision_type == "edit_and_approve":
            if request.to_participants is not None:
                return {}, collab_error(
                    "invalid_payload",
                    "edit_and_approve cannot carry redirected recipients",
                    None,
                )
            content = (request.final_content_text or "").strip()
            if not content:
                return {}, collab_error(
                    "invalid_payload",
                    "final_content_text is required for edit_and_approve",
                    None,
                )
            return {
                "payload_json": (
                    draft_payload
                    if request.final_payload_json is None
                    else request.final_payload_json
                ),
                "content_text": content,
                "recipients": draft_recipients,
                "deliverable": True,
            }, None
        if request.decision_type == "redirect_and_approve":
            if request.final_payload_json is not None or request.final_content_text is not None:
                return {}, collab_error(
                    "invalid_payload",
                    "redirect_and_approve cannot carry edited final content or payload",
                    None,
                )
            if request.to_participants is None:
                return {}, collab_error(
                    "invalid_payload",
                    "to_participants is required for redirect_and_approve",
                    None,
                )
            recipients, error = self._resolve_recipients(request.to_participants)
            if error is not None:
                return {}, error
            return {
                "payload_json": draft_payload,
                "content_text": draft["content_text"],
                "recipients": recipients,
                "deliverable": True,
            }, None
        if (
            request.final_payload_json is not None
            or request.final_content_text is not None
            or request.to_participants is not None
        ):
            return {}, collab_error(
                "invalid_payload",
                "approve cannot carry final content, final payload, or redirected recipients",
                None,
            )
        return {
            "payload_json": draft_payload,
            "content_text": draft["content_text"],
            "recipients": draft_recipients,
            "deliverable": True,
        }, None

    def _decision_matches(
        self,
        conn: sqlite3.Connection,
        decision: dict[str, Any],
        normalized: dict[str, Any],
        request: OperatorDecisionRequest,
    ) -> bool:
        return (
            decision["decision_type"] == request.decision_type
            and collab_store.nullable_payload_from_row(decision) == normalized["payload_json"]
            and decision["final_content_text"] == normalized["content_text"]
            and decision["reason"] == request.reason
            and self._recipient_ids(
                collab_store.list_decision_recipients(conn, decision["decision_id"])
            )
            == tuple(item.participant_id for item in normalized["recipients"])
        )

    def _participants_from_rows(self, rows: list[dict[str, Any]]) -> tuple[ParticipantRef, ...]:
        participants = []
        for row in rows:
            resolved = self._identity.resolve(row["recipient_participant"])
            if resolved is None:
                raise RuntimeError(f"stored participant {row['recipient_participant']!r} not found")
            participants.append(resolved)
        return tuple(participants)

    def _recipient_ids(self, rows: list[dict[str, Any]]) -> tuple[str, ...]:
        return tuple(row["recipient_participant"] for row in rows)

    def _draft_from_row(self, conn: sqlite3.Connection, row: dict[str, Any]) -> CollabDraft:
        sender = self._identity.resolve(row["from_participant"])
        if sender is None:
            raise RuntimeError(f"stored participant {row['from_participant']!r} not found")
        return CollabDraft(
            draft_id=row["draft_id"],
            thread_id=row["thread_id"],
            from_participant=sender,
            to_participants=self._participants_from_rows(
                collab_store.list_draft_recipients(conn, row["draft_id"])
            ),
            message_kind=row["kind"],
            payload_json=collab_store.payload_from_row(row),
            content_text=row["content_text"],
            correlation_id=row["correlation_id"],
            created_ts=row["created_ts"],
        )

    def _decision_from_row(self, row: dict[str, Any]) -> CollabDecision:
        return CollabDecision(
            decision_id=row["decision_id"],
            draft_id=row["draft_id"],
            decision_type=row["decision_type"],
            operator_participant=row["operator_participant"],
            final_payload_json=collab_store.nullable_payload_from_row(row),
            final_content_text=row["final_content_text"],
            reason=row["reason"],
            decision_ts=row["decision_ts"],
        )

    def _deliverable_from_row(
        self,
        conn: sqlite3.Connection,
        row: dict[str, Any] | None,
    ) -> CollabDeliverable | None:
        if row is None:
            return None
        sender = self._identity.resolve(row["from_participant"])
        if sender is None:
            raise RuntimeError(f"stored participant {row['from_participant']!r} not found")
        return CollabDeliverable(
            deliverable_id=row["deliverable_id"],
            draft_id=row["draft_id"],
            decision_id=row["decision_id"],
            thread_id=row["thread_id"],
            from_participant=sender,
            to_participants=self._participants_from_rows(
                collab_store.list_receipts_for_deliverable(conn, row["deliverable_id"])
            ),
            message_kind=row["kind"],
            payload_json=collab_store.payload_from_row(row),
            content_text=row["content_text"],
            correlation_id=row["correlation_id"],
            created_ts=row["created_ts"],
        )

    def _thread_entries(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        participant: ParticipantRef,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        is_operator = participant.participant_id == self._operator_participant_id
        for draft_row in collab_store.list_thread_drafts(conn, thread_id):
            draft = self._draft_from_row(conn, draft_row)
            if is_operator or draft.from_participant.participant_id == participant.participant_id:
                entries.append({"entry_type": "draft", "draft": draft.model_dump(mode="json")})
                decision = collab_store.get_decision_for_draft(conn, draft.draft_id)
                if decision is not None:
                    entries.append(
                        {
                            "entry_type": "decision",
                            "decision": self._decision_from_row(decision).model_dump(mode="json"),
                        }
                    )
        for deliverable_row in collab_store.list_thread_deliverables(conn, thread_id):
            deliverable = self._deliverable_from_row(conn, deliverable_row)
            if deliverable is None:
                continue
            recipient_ids = {item.participant_id for item in deliverable.to_participants}
            if (
                is_operator
                or deliverable.from_participant.participant_id == participant.participant_id
                or participant.participant_id in recipient_ids
            ):
                entries.append(
                    {
                        "entry_type": "deliverable",
                        "deliverable": deliverable.model_dump(mode="json"),
                    }
                )
        entries.extend(self._audit_event_entries(conn, entries))
        return entries

    def _draft_has_probe_participant(self, draft: CollabDraft) -> bool:
        if draft.from_participant.is_probe:
            return True
        return any(item.is_probe for item in draft.to_participants)

    def _participant_can_read_thread(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        participant: ParticipantRef,
    ) -> bool:
        if participant.participant_id == self._operator_participant_id:
            return True
        for draft_row in collab_store.list_thread_drafts(conn, thread_id):
            if draft_row["from_participant"] == participant.participant_id:
                return True
            if participant.participant_id in self._recipient_ids(
                collab_store.list_draft_recipients(conn, draft_row["draft_id"])
            ):
                return True
        for deliverable_row in collab_store.list_thread_deliverables(conn, thread_id):
            if deliverable_row["from_participant"] == participant.participant_id:
                return True
            if participant.participant_id in self._recipient_ids(
                collab_store.list_receipts_for_deliverable(
                    conn,
                    deliverable_row["deliverable_id"],
                )
            ):
                return True
        return False

    def _audit_event_entries(
        self,
        conn: sqlite3.Connection,
        visible_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        draft_ids = []
        decision_ids = []
        deliverable_ids = []
        for entry in visible_entries:
            if entry["entry_type"] == "draft":
                draft_ids.append(entry["draft"]["draft_id"])
            elif entry["entry_type"] == "decision":
                decision_ids.append(entry["decision"]["decision_id"])
            elif entry["entry_type"] == "deliverable":
                deliverable_ids.append(entry["deliverable"]["deliverable_id"])
        return [
            {
                "entry_type": "audit_event",
                "event": {
                    "event_id": row["event_id"],
                    "draft_id": row["draft_id"],
                    "decision_id": row["decision_id"],
                    "deliverable_id": row["deliverable_id"],
                    "participant_id": row["participant_id"],
                    "event_kind": row["event_kind"],
                    "event_ts": row["event_ts"],
                    "detail_json": collab_store.detail_from_row(row),
                },
            }
            for row in collab_store.list_events_for_refs(
                conn,
                draft_ids=draft_ids,
                decision_ids=decision_ids,
                deliverable_ids=deliverable_ids,
            )
        ]
